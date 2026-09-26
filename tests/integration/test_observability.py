from datetime import UTC, datetime, timedelta

import pytest
from pydantic import SecretStr

from fmaiily.container import build_container
from fmaiily.metrics import Metrics
from fmaiily.models import SendEvent, SendJob
from fmaiily.worker import Worker
from tests.support.fake_gmail_app import fake_gmail_app
from tests.support.sync_asgi import sync_asgi_client

NOW = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)


@pytest.fixture
def metrics() -> Metrics:
    return Metrics()


@pytest.fixture
def wired(settings, seeded_engine, fake_clock, sleeper, fake_transport, metrics):
    tuned = settings.model_copy(
        update={
            "google_oauth_client_id": "cid",
            "google_oauth_client_secret": SecretStr("csecret"),
            "oauth_token_uri": "http://oauth.test/token",
            "oidc_userinfo_url": "http://oauth.test/v1/userinfo",
        }
    )
    container = build_container(
        tuned,
        engine=seeded_engine,
        transport=fake_transport,
        clock=fake_clock,
        sleeper=sleeper,
        http=sync_asgi_client(fake_gmail_app(), base_url="http://oauth.test"),
    )
    container.metrics = metrics
    return container


@pytest.fixture
def connected(wired) -> str:
    start = wired.oauth.start()
    return wired.oauth.callback(code="code", state=start.state).email


def _enqueue(container, now: datetime = NOW) -> int:
    account = container.accounts.resolve(None)
    job = container.queue.enqueue(
        account=account,
        payload=b"From: me@example.com\r\n\r\nbody",
        recipients=1,
        source="api",
        now=now,
    )
    return job.id


# ----------------------------------------------------------------- send path


def test_a_successful_send_is_counted(wired, connected, metrics) -> None:
    worker = Worker(wired, worker_id="w", rand=lambda: 0.0)
    _enqueue(wired)
    assert worker.run_once(now=NOW).action == "sent"
    text = metrics.render()
    assert (
        'fmaiily_sends_total{account="sender@example.com",outcome="sent",source="api"} 1.0' in text
    )
    assert 'fmaiily_worker_iterations_total{action="sent"} 1.0' in text


def test_a_failed_send_is_counted_with_its_code(wired, connected, metrics, fake_transport) -> None:
    from fmaiily.gmail.client import GoogleApiError

    fake_transport.script_error(
        GoogleApiError(status_code=403, reason="dailySendQuotaExceeded", message="limit")
    )
    worker = Worker(wired, worker_id="w", rand=lambda: 0.0)
    _enqueue(wired)
    assert worker.run_once(now=NOW).action == "failed"
    text = metrics.render()
    assert 'outcome="failed"' in text
    assert 'error_code="daily_send_quota_exceeded"' in text


def test_send_latency_is_observed(wired, connected, metrics) -> None:
    worker = Worker(wired, worker_id="w", rand=lambda: 0.0)
    _enqueue(wired)
    worker.run_once(now=NOW)
    assert "fmaiily_send_duration_seconds_count" in metrics.render()


def test_token_refreshes_are_counted(wired, connected, metrics) -> None:
    wired.accounts.expire_token(connected)
    worker = Worker(wired, worker_id="w", rand=lambda: 0.0)
    _enqueue(wired)
    worker.run_once(now=NOW)
    assert 'fmaiily_tokens_refreshed_total{account="sender@example.com",result="ok"} 1.0' in (
        metrics.render()
    )


def test_idle_iterations_are_counted(wired, metrics) -> None:
    worker = Worker(wired, worker_id="w", rand=lambda: 0.0)
    assert worker.run_once(now=NOW).action == "idle"
    assert 'fmaiily_worker_iterations_total{action="idle"} 1.0' in metrics.render()


def test_queue_depth_and_quota_gauges_are_published(wired, connected, metrics) -> None:
    worker = Worker(wired, worker_id="w", rand=lambda: 0.0)
    _enqueue(wired)
    worker.run_once(now=NOW)
    text = metrics.render()
    assert "fmaiily_queue_depth 0.0" in text
    assert 'fmaiily_quota_remaining{account="sender@example.com",resource="messages"}' in text


# ----------------------------------------------------------------- retention


def _expire_payload(container, job_id: int) -> None:
    with container.session_factory() as session:
        row = session.get(SendJob, job_id)
        row.raw_payload_enc = _real_payload(container, job_id)
        row.payload_expires_at = NOW - timedelta(hours=1)
        session.commit()


def _real_payload(container, job_id: int) -> str:
    """The payload as it looked before the job reached a terminal state."""
    with container.session_factory() as session:
        row = session.get(SendJob, job_id)
        return row.raw_payload_enc or ""


def test_maintenance_sweeps_expired_payloads(wired, connected) -> None:
    job_id = _enqueue(wired)
    worker = Worker(wired, worker_id="w", rand=lambda: 0.0)
    worker.run_once(now=NOW)
    assert wired.queue.get(job_id).raw_payload_enc is None  # wiped on the terminal state

    # a job that failed long ago and still holds a payload past its retention window
    with wired.session_factory() as session:
        row = session.get(SendJob, job_id)
        row.raw_payload_enc = _payload_for(container=wired)
        row.payload_expires_at = NOW - timedelta(hours=1)
        session.commit()
    report = worker.run_maintenance(now=NOW)
    assert report["payloads_swept"] == 1
    assert wired.queue.get(job_id).raw_payload_enc is None


def _payload_for(*, container) -> str:
    from fmaiily.gmail.mime import OutgoingMessage, build_mime, to_raw_b64

    raw = build_mime(
        OutgoingMessage(
            from_email="sender@example.com", to=("a@example.com",), subject="s", body="b"
        ),
        now=NOW,
    )
    return container.cipher.encrypt(to_raw_b64(raw), aad="1")


def test_maintenance_purges_old_history(wired, connected, settings) -> None:
    history = wired.history
    account = wired.accounts.resolve(None)
    with wired.session_factory() as session:
        for _ in range(3):
            session.add(
                SendJob(
                    account_id=account.id,
                    status="sent",
                    recipients=1,
                    source="api",
                    scheduled_at=NOW - timedelta(days=90),
                    sent_at=NOW - timedelta(days=90),
                    created_at=NOW - timedelta(days=90),
                    updated_at=NOW - timedelta(days=90),
                )
            )
        session.commit()
    removed = history.purge_older_than(days=settings.history_retention_days, now=NOW)
    assert removed == 3
    assert history.list_recent() == []


def test_maintenance_keeps_active_jobs(wired, connected) -> None:
    _enqueue(wired)
    assert wired.history.purge_older_than(days=0, now=NOW) == 0
    assert len(wired.history.list_recent()) == 1


def test_maintenance_deletes_the_events_of_purged_jobs(wired, connected) -> None:
    account = wired.accounts.resolve(None)
    with wired.session_factory() as session:
        job = SendJob(
            account_id=account.id,
            status="sent",
            recipients=1,
            source="api",
            scheduled_at=NOW - timedelta(days=90),
            sent_at=NOW - timedelta(days=90),
            created_at=NOW - timedelta(days=90),
            updated_at=NOW - timedelta(days=90),
        )
        session.add(job)
        session.flush()
        job_id = job.id
        session.add(
            SendEvent(
                job_id=job_id,
                account_id=account.id,
                event="succeeded",
                created_at=NOW - timedelta(days=90),
            )
        )
        session.commit()
    wired.history.purge_older_than(days=30, now=NOW)
    with wired.session_factory() as session:
        assert session.query(SendEvent).filter_by(job_id=job_id).count() == 0
        assert session.query(SendJob).filter_by(id=job_id).count() == 0
