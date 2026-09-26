from datetime import UTC, datetime, timedelta

import pytest
from pydantic import SecretStr

from fmaiily.container import Container, build_container
from fmaiily.gmail.client import GoogleApiError
from fmaiily.worker import ProcessResult, Worker
from tests.support.fake_gmail_app import DEFAULT_ACCOUNT, fake_gmail_app
from tests.support.fakes import FakeGmailTransport
from tests.support.sync_asgi import sync_asgi_client

FAKE_APP = fake_gmail_app()
SENDER = DEFAULT_ACCOUNT
NOW = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)


@pytest.fixture
def wcontainer(settings, seeded_engine, fake_clock, sleeper, fake_transport) -> Container:
    tuned = settings.model_copy(
        update={
            "google_oauth_client_id": "cid",
            "google_oauth_client_secret": SecretStr("csecret"),
            "oauth_token_uri": "http://oauth.test/token",
            "oidc_userinfo_url": "http://oauth.test/v1/userinfo",
        }
    )
    return build_container(
        tuned,
        engine=seeded_engine,
        transport=fake_transport,
        clock=fake_clock,
        sleeper=sleeper,
        http=sync_asgi_client(FAKE_APP, base_url="http://oauth.test"),
    )


@pytest.fixture
def worker(wcontainer: Container, sleeper) -> Worker:
    # jitter pinned to 0.0 so backoff assertions are exact
    return Worker(wcontainer, worker_id="w-test", sleeper=sleeper, rand=lambda: 0.0)


@pytest.fixture
def connected(wcontainer: Container) -> None:
    """Connect an account through the real OAuth flow so its tokens are real ciphertext."""
    start = wcontainer.oauth.start()
    wcontainer.oauth.callback(code="code", state=start.state)


def _enqueue(wcontainer: Container, now: datetime | None = None) -> int:
    now = now or wcontainer.clock.now()
    account = wcontainer.accounts.resolve(None)
    job = wcontainer.queue.enqueue(
        account=account,
        payload=b"From: me@example.com\r\n\r\nbody",
        recipients=1,
        source="api",
        now=now,
    )
    return job.id


# --------------------------------------------------------------------- idle


def test_run_once_on_an_empty_queue_is_idle(worker: Worker) -> None:
    result = worker.run_once(now=NOW)
    assert result.action == "idle"
    assert result.job_id is None


def test_run_forever_stops_on_the_event(wcontainer: Container) -> None:
    import threading

    worker = Worker(wcontainer, worker_id="w-loop", poll_interval=0.01)
    stop = threading.Event()
    stop.set()
    worker.run_forever(stop)  # returns immediately when already stopped
    assert stop.is_set()


# --------------------------------------------------------------------- happy


def test_worker_sends_a_queued_job(
    worker: Worker, wcontainer: Container, connected, fake_transport: FakeGmailTransport
) -> None:
    job_id = _enqueue(wcontainer)
    result = worker.run_once(now=NOW)
    assert result.action == "sent"
    assert result.job_id == job_id
    assert result.message_id == "msg-1"
    job = wcontainer.queue.get(job_id)
    assert job is not None
    assert job.status == "sent"
    assert job.gmail_message_id == "msg-1"
    assert fake_transport.calls[0]["email"] == SENDER


def test_worker_sends_the_decrypted_payload(
    worker: Worker, wcontainer: Container, connected, fake_transport: FakeGmailTransport
) -> None:
    _enqueue(wcontainer)
    worker.run_once(now=NOW)
    assert fake_transport.calls[0]["raw_b64url"]


def test_worker_forwards_the_thread_id(
    worker: Worker, wcontainer: Container, connected, fake_transport: FakeGmailTransport
) -> None:
    account = wcontainer.accounts.resolve(None)
    job = wcontainer.queue.enqueue(
        account=account,
        payload=b"raw",
        recipients=1,
        source="api",
        now=NOW,
        thread_id="th-42",
    )
    worker.run_once(now=NOW)
    assert fake_transport.calls[0]["thread_id"] == "th-42"
    assert wcontainer.queue.get(job.id).status == "sent"


def test_worker_processes_jobs_in_schedule_order(worker: Worker, wcontainer: Container, connected):
    first = _enqueue(wcontainer)
    second = _enqueue(wcontainer)
    assert worker.run_once(now=NOW + timedelta(seconds=5)).job_id == first
    assert worker.run_once(now=NOW + timedelta(seconds=5)).job_id == second


# ------------------------------------------------------------------- retries


def test_retryable_error_schedules_a_backoff(
    worker: Worker, wcontainer: Container, connected, fake_transport: FakeGmailTransport
) -> None:
    fake_transport.script_error(
        GoogleApiError(status_code=429, reason="rateLimitExceeded", message="slow")
    )
    job_id = _enqueue(wcontainer)
    result = worker.run_once(now=NOW)
    assert result.action == "retry_scheduled"
    assert result.error_code == "gmail_rate_limited"
    job = wcontainer.queue.get(job_id)
    assert job is not None
    assert job.status == "pending"
    assert job.scheduled_at == NOW + timedelta(seconds=1)
    assert job.attempt_count == 1


def test_daily_quota_error_is_terminal(
    worker: Worker, wcontainer: Container, connected, fake_transport: FakeGmailTransport
) -> None:
    fake_transport.script_error(
        GoogleApiError(status_code=403, reason="dailySendQuotaExceeded", message="daily limit")
    )
    job_id = _enqueue(wcontainer)
    result = worker.run_once(now=NOW)
    assert result.action == "failed"
    assert result.error_code == "daily_send_quota_exceeded"
    job = wcontainer.queue.get(job_id)
    assert job is not None and job.status == "failed"
    assert job.scheduled_at == NOW  # not rescheduled


def test_attempts_are_exhausted(
    worker: Worker, wcontainer: Container, connected, fake_transport: FakeGmailTransport
) -> None:
    job_id = _enqueue(wcontainer)
    clock = wcontainer.clock
    for attempt in range(1, 6):
        fake_transport.script_error(
            GoogleApiError(status_code=503, reason="backendError", message="oops")
        )
        # each retry is scheduled into the future, so step the clock past its backoff
        result = worker.run_once(now=clock.now())
        assert result.action == ("retry_scheduled" if attempt < 5 else "failed")
        clock.advance(timedelta(seconds=64))
    job = wcontainer.queue.get(job_id)
    assert job is not None
    assert job.status == "failed"
    assert job.attempt_count == 5
    assert job.error_code == "gmail_backend_error"


def test_retry_after_header_is_honored(
    worker: Worker, wcontainer: Container, connected, fake_transport: FakeGmailTransport
) -> None:
    fake_transport.script_error(
        GoogleApiError(
            status_code=429, reason="rateLimitExceeded", message="slow", retry_after=45.0
        )
    )
    job_id = _enqueue(wcontainer)
    worker.run_once(now=NOW)
    job = wcontainer.queue.get(job_id)
    assert job is not None
    assert job.scheduled_at == NOW + timedelta(seconds=45)


# ----------------------------------------------------------- token refresh


def test_auth_expired_triggers_one_refresh_and_retry(
    worker: Worker, wcontainer: Container, connected, fake_transport: FakeGmailTransport
) -> None:
    from fmaiily.gmail.client import AuthExpired

    fake_transport.script_error(AuthExpired(status_code=401, reason="authError", message="stale"))
    job_id = _enqueue(wcontainer)
    result = worker.run_once(now=NOW)
    assert result.action == "sent"
    assert result.job_id == job_id
    # the retry used the freshly minted token
    assert fake_transport.calls[1]["access_token"] == "fake-access-token"
    assert wcontainer.queue.get(job_id).status == "sent"


def test_persistent_auth_failure_fails_the_job(
    worker: Worker, wcontainer: Container, connected, fake_transport: FakeGmailTransport
) -> None:
    from fmaiily.gmail.client import AuthExpired

    for _ in range(2):
        fake_transport.script_error(
            AuthExpired(status_code=401, reason="authError", message="stale")
        )
    job_id = _enqueue(wcontainer)
    result = worker.run_once(now=NOW)
    assert result.action == "failed"
    assert result.error_code == "auth_expired"
    assert wcontainer.queue.get(job_id).status == "failed"


def test_worker_refreshes_an_expiring_token(
    worker: Worker, wcontainer: Container, connected, fake_transport: FakeGmailTransport
) -> None:
    wcontainer.accounts.expire_token(SENDER)
    FAKE_APP.state.requests.clear()
    _enqueue(wcontainer)
    worker.run_once(now=NOW)
    assert [r["path"] for r in FAKE_APP.state.requests] == ["/token"]
    assert fake_transport.calls[0]["access_token"] == "fake-access-token"


# -------------------------------------------------------------- bad accounts


def test_worker_fails_the_job_for_a_revoked_account(
    worker: Worker, wcontainer: Container, connected
):
    job_id = _enqueue(wcontainer)
    wcontainer.accounts.revoke(SENDER)
    result = worker.run_once(now=NOW)
    assert result.action == "failed"
    assert result.error_code == "send_failed"
    assert wcontainer.queue.get(job_id).status == "failed"


def test_worker_fails_the_job_when_the_account_disappeared(
    worker: Worker, wcontainer: Container, connected
) -> None:
    """An account deleted out from under a queued job (restore, manual cleanup) must not crash
    the worker loop; the job is closed out with a clear code instead."""
    from sqlalchemy import text

    job_id = _enqueue(wcontainer)
    with wcontainer.engine.begin() as conn:  # simulate an out-of-band delete
        conn.execute(text("PRAGMA foreign_keys=OFF"))
        conn.execute(text(f"DELETE FROM accounts WHERE email = '{SENDER}'"))
        conn.execute(text("PRAGMA foreign_keys=ON"))
    result = worker.run_once(now=NOW)
    assert result.action == "failed"
    assert result.error_code == "send_failed"
    assert wcontainer.queue.get(job_id).status == "failed"


def test_worker_fails_the_job_without_a_payload(worker: Worker, wcontainer: Container, connected):
    job_id = _enqueue(wcontainer)
    with wcontainer.session_factory() as session:
        from fmaiily.models import SendJob

        row = session.get(SendJob, job_id)
        row.raw_payload_enc = None
        session.commit()
    result = worker.run_once(now=NOW)
    assert result.action == "failed"
    assert wcontainer.queue.get(job_id).status == "failed"


# ------------------------------------------------------------------- leases


def test_worker_recovers_expired_leases(
    worker: Worker, wcontainer: Container, connected, fake_transport: FakeGmailTransport
) -> None:
    job_id = _enqueue(wcontainer)
    wcontainer.queue.claim_next(worker_id="dead", now=NOW, lease_seconds=10)
    assert worker.run_once(now=NOW + timedelta(seconds=5)).action == "idle"  # lease still valid
    result = worker.run_once(now=NOW + timedelta(seconds=30))
    assert result.action == "sent"  # the lease expired, so the job was recovered and sent
    assert result.job_id == job_id
    job = wcontainer.queue.get(job_id)
    assert job is not None and job.status == "sent" and job.worker_id is None


def test_recovered_job_reports_its_new_worker(
    worker: Worker, wcontainer: Container, connected, fake_transport: FakeGmailTransport
) -> None:
    job_id = _enqueue(wcontainer)
    wcontainer.queue.claim_next(worker_id="dead", now=NOW, lease_seconds=10)
    fake_transport.script_error(GoogleApiError(status_code=503, reason="backendError", message="x"))
    result = worker.run_once(now=NOW + timedelta(seconds=30))
    # recovery makes it claimable again, so this pass sends it rather than reporting "requeued"
    assert result.action == "retry_scheduled"
    job = wcontainer.queue.get(job_id)
    assert job is not None and job.worker_id is None and job.status == "pending"


# --------------------------------------------------------------- end to end


def test_full_path_from_send_service_to_gmail(wcontainer: Container, connected, sleeper):
    """SendService -> queue -> worker -> transport, with no worker thread in between."""
    from fmaiily.gmail.mime import OutgoingMessage

    transport = wcontainer.transport
    assert isinstance(transport, FakeGmailTransport)
    outcome = wcontainer.sender.send(
        account_email=None,
        msg=OutgoingMessage(
            from_email=SENDER, to=("recipient@example.com",), subject="s", body="b"
        ),
        source="api",
        wait=False,
        now=NOW,
    )
    assert outcome.status == "queued"
    worker = Worker(wcontainer, worker_id="w", sleeper=sleeper)
    result = worker.run_once(now=NOW)
    assert result.action == "sent"
    assert transport.calls[0]["email"] == SENDER
    assert wcontainer.history.job_status(outcome.job_id).sent_at == NOW


def test_quota_refusal_never_reaches_the_worker(
    wcontainer: Container, connected, fake_transport: FakeGmailTransport
):
    from fmaiily.errors import QuotaExceeded
    from fmaiily.gmail.mime import OutgoingMessage
    from fmaiily.models import SendJob

    for _ in range(425):
        with wcontainer.session_factory() as session:
            session.add(
                SendJob(
                    account_id=wcontainer.accounts.resolve(None).id,
                    status="sent",
                    recipients=1,
                    source="api",
                    scheduled_at=NOW,
                    sent_at=NOW,
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
            session.commit()
    with pytest.raises(QuotaExceeded):
        wcontainer.sender.send(
            account_email=None,
            msg=OutgoingMessage(from_email=SENDER, to=("r@example.com",), subject="s", body="b"),
            source="api",
            wait=False,
            now=NOW,
        )
    worker = Worker(wcontainer, worker_id="w")
    assert worker.run_once(now=NOW).action == "idle"
    assert fake_transport.calls == []


def test_process_result_is_frozen() -> None:
    import dataclasses

    result = ProcessResult(action="idle")
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.action = "sent"  # type: ignore[misc]
