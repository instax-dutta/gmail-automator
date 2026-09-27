import threading
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from gmail_automator.accounts import AccountService
from gmail_automator.accounts import AccountService as _AccountService
from gmail_automator.config import Settings
from gmail_automator.crypto import TokenCipher
from gmail_automator.errors import DuplicateRequest, QueueFull
from gmail_automator.gmail.client import SendResult
from gmail_automator.models import Account, SendEvent, SendJob
from gmail_automator.queue import QueueService

NOW = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
PAYLOAD = b"From: me@example.com\r\nSubject: hi\r\n\r\nbody"
RESULT = SendResult(message_id="gmail-1", thread_id="thread-1", label_ids=("SENT",))


@pytest.fixture
def accounts(session_factory, seeded_engine, fake_clock, settings) -> AccountService:
    return _AccountService(session_factory=session_factory, clock=fake_clock, settings=settings)


@pytest.fixture
def account(accounts: AccountService) -> Account:
    return accounts.upsert_oauth_account(
        email="me@example.com",
        scopes=["https://www.googleapis.com/auth/gmail.send"],
        token_uri="https://oauth2.googleapis.com/token",
        now=NOW,
    )


@pytest.fixture
def cipher(settings) -> TokenCipher:
    return TokenCipher(settings.encryption_key_bytes())


@pytest.fixture
def queue(
    session_factory, seeded_engine, fake_clock, cipher, settings: Settings, sleeper
) -> QueueService:
    return QueueService(
        session_factory=session_factory,
        cipher=cipher,
        clock=fake_clock,
        settings=settings,
        sleeper=sleeper,
    )


def _enqueue(queue: QueueService, account: Account, **kwargs) -> SendJob:
    payload = kwargs.pop("payload", PAYLOAD)
    return queue.enqueue(
        account=account,
        payload=payload,
        recipients=kwargs.pop("recipients", 1),
        source=kwargs.pop("source", "api"),
        now=kwargs.pop("now", NOW),
        **kwargs,
    )


# ------------------------------------------------------------------ enqueue


def test_enqueue_creates_a_pending_job(queue: QueueService, account: Account) -> None:
    job = _enqueue(queue, account)
    assert job.id is not None
    assert job.status == "pending"
    assert job.account_id == account.id
    assert job.recipients == 1
    assert job.source == "api"
    assert job.attempt_count == 0
    assert job.max_attempts == 5
    assert job.scheduled_at == NOW
    assert job.lease_expires_at is None
    assert job.worker_id is None


def test_payload_is_encrypted_and_bound_to_the_job_id(
    queue: QueueService, account: Account, cipher: TokenCipher
) -> None:
    job = _enqueue(queue, account)
    assert job.raw_payload_enc is not None
    assert "From: me@example.com" not in job.raw_payload_enc
    # the stored form is base64url of the payload, wrapped in the versioned ciphertext envelope
    assert cipher.decrypt(job.raw_payload_enc, aad=str(job.id)).startswith("RnJvbTog")
    assert queue.decrypt_payload(job) == PAYLOAD


def test_enqueue_records_an_event(queue: QueueService, account: Account, session_factory) -> None:
    job = _enqueue(queue, account)
    with session_factory() as session:
        events = list(session.scalars(select(SendEvent).where(SendEvent.job_id == job.id)))
    assert [e.event for e in events] == ["enqueued"]


def test_enqueue_sets_a_payload_expiry(queue: QueueService, account: Account) -> None:
    job = _enqueue(queue, account)
    assert job.payload_expires_at == NOW + timedelta(hours=24)


def test_pacing_schedules_the_second_send_behind_the_first(
    queue: QueueService, account: Account, accounts: AccountService
) -> None:
    first = _enqueue(queue, account)
    second = _enqueue(queue, account)
    assert first.scheduled_at == NOW
    assert second.scheduled_at == NOW + timedelta(seconds=2)
    assert accounts.get("me@example.com").next_send_at == NOW + timedelta(seconds=4)


def test_pacing_is_not_delayed_by_a_cursor_in_the_past(
    queue: QueueService, account: Account, accounts: AccountService
) -> None:
    accounts.advance_pacing("me@example.com", next_send_at=NOW - timedelta(hours=1))
    job = _enqueue(queue, account, now=NOW)
    assert job.scheduled_at == NOW


def test_queue_depth_limit_raises_queue_full(
    queue: QueueService, account: Account, settings: Settings
) -> None:
    small = settings.model_copy(update={"queue_max_depth": 2})
    limited = QueueService(
        session_factory=queue._session_factory,
        cipher=queue._cipher,
        clock=queue._clock,
        settings=small,
        sleeper=queue._sleeper,
    )
    _enqueue(limited, account)
    _enqueue(limited, account)
    with pytest.raises(QueueFull) as excinfo:
        _enqueue(limited, account)
    assert excinfo.value.code == "queue_full"
    assert excinfo.value.details["account"] == "me@example.com"


def test_schedule_horizon_raises_queue_full(
    queue: QueueService, account: Account, settings: Settings
) -> None:
    short = settings.model_copy(update={"max_schedule_horizon_hours": 0.0})
    limited = QueueService(
        session_factory=queue._session_factory,
        cipher=queue._cipher,
        clock=queue._clock,
        settings=short,
        sleeper=queue._sleeper,
    )
    _enqueue(limited, account, now=NOW)  # first job lands exactly on `now`
    # pacing pushes the second job 2s past `now`, outside a zero horizon
    with pytest.raises(QueueFull) as excinfo:
        _enqueue(limited, account, now=NOW)
    assert excinfo.value.details["horizon_hours"] == 0.0


def test_duplicate_idempotency_key_is_rejected(queue: QueueService, account: Account) -> None:
    _enqueue(queue, account, idempotency_key="abc", idempotency_scope="key:1")
    with pytest.raises(DuplicateRequest) as excinfo:
        _enqueue(queue, account, idempotency_key="abc", idempotency_scope="key:1")
    assert excinfo.value.code == "duplicate_request"


def test_same_idempotency_key_from_a_different_client_is_allowed(
    queue: QueueService, account: Account
) -> None:
    _enqueue(queue, account, idempotency_key="abc", idempotency_scope="key:1")
    job = _enqueue(queue, account, idempotency_key="abc", idempotency_scope="key:2")
    assert job.id is not None


# -------------------------------------------------------------------- claim


def test_claim_next_returns_the_oldest_due_job(queue: QueueService, account: Account) -> None:
    first = _enqueue(queue, account)
    second = _enqueue(queue, account)
    claimed = queue.claim_next(worker_id="w1", now=NOW + timedelta(seconds=5), lease_seconds=60)
    assert claimed is not None
    assert claimed.id == first.id
    assert second.id != first.id


def test_claim_marks_processing_and_takes_a_lease(queue: QueueService, account: Account) -> None:
    job = _enqueue(queue, account)
    claimed = queue.claim_next(worker_id="w1", now=NOW, lease_seconds=60)
    assert claimed is not None
    assert claimed.status == "processing"
    assert claimed.worker_id == "w1"
    assert claimed.lease_expires_at == NOW + timedelta(seconds=60)
    assert claimed.attempt_count == 1
    assert job.id == claimed.id


def test_claim_increments_the_attempt_counter(queue: QueueService, account: Account) -> None:
    _enqueue(queue, account)
    queue.claim_next(worker_id="w1", now=NOW, lease_seconds=60)
    queue.mark_failed(job_id=1, error_code="send_failed", message="boom", now=NOW)
    queue.reschedule(job_id=1, delay_seconds=0, error_code="send_failed", message="boom", now=NOW)
    again = queue.claim_next(worker_id="w1", now=NOW, lease_seconds=60)
    assert again is not None
    assert again.attempt_count == 2


def test_claim_skips_jobs_scheduled_in_the_future(queue: QueueService, account: Account) -> None:
    due = _enqueue(queue, account)  # scheduled now
    paced = _enqueue(queue, account)  # paced 2s later
    claimed = queue.claim_next(worker_id="w1", now=NOW, lease_seconds=60)
    assert claimed is not None and claimed.id == due.id
    assert queue.claim_next(worker_id="w1", now=NOW, lease_seconds=60) is None
    later = queue.claim_next(worker_id="w1", now=NOW + timedelta(seconds=2), lease_seconds=60)
    assert later is not None and later.id == paced.id


def test_claim_returns_none_on_an_empty_queue(queue: QueueService) -> None:
    assert queue.claim_next(worker_id="w1", now=NOW, lease_seconds=60) is None


def test_a_leased_job_is_not_claimed_twice(queue: QueueService, account: Account) -> None:
    _enqueue(queue, account)
    assert queue.claim_next(worker_id="w1", now=NOW, lease_seconds=60) is not None
    assert queue.claim_next(worker_id="w2", now=NOW, lease_seconds=60) is None


def test_concurrent_workers_never_claim_the_same_job(queue: QueueService, account: Account) -> None:
    for _ in range(5):
        _enqueue(queue, account, now=NOW - timedelta(hours=1))  # all already due

    claimed: list[int | None] = []
    lock = threading.Lock()

    def drain(worker_id: str) -> None:
        while True:
            job = queue.claim_next(worker_id=worker_id, now=NOW, lease_seconds=60)
            with lock:
                claimed.append(job.id if job is not None else None)
            if job is None:
                return

    threads = [threading.Thread(target=drain, args=(f"w{i}",)) for i in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    ids = [job_id for job_id in claimed if job_id is not None]
    assert sorted(ids) == sorted(set(ids))  # no double claim
    assert len(ids) == 5
    assert claimed.count(None) == 5  # every worker eventually found the queue empty
    assert queue.claim_next(worker_id="w9", now=NOW, lease_seconds=60) is None


def test_claim_records_a_claimed_event(
    queue: QueueService, account: Account, session_factory
) -> None:
    job = _enqueue(queue, account)
    queue.claim_next(worker_id="w1", now=NOW, lease_seconds=60)
    with session_factory() as session:
        events = list(
            session.scalars(
                select(SendEvent).where(SendEvent.job_id == job.id).order_by(SendEvent.id)
            )
        )
    assert [e.event for e in events] == ["enqueued", "claimed"]
    assert events[-1].attempt == 1


# ------------------------------------------------------------------ outcomes


def test_mark_sent_records_the_gmail_ids(queue: QueueService, account: Account) -> None:
    job = _enqueue(queue, account)
    queue.claim_next(worker_id="w1", now=NOW, lease_seconds=60)
    queue.mark_sent(job_id=job.id, result=RESULT, now=NOW + timedelta(seconds=1))
    stored = queue.get(job.id)
    assert stored is not None
    assert stored.status == "sent"
    assert stored.gmail_message_id == "gmail-1"
    assert stored.gmail_thread_id == "thread-1"
    assert stored.sent_at == NOW + timedelta(seconds=1)
    assert stored.finished_at == NOW + timedelta(seconds=1)
    assert stored.lease_expires_at is None
    assert stored.worker_id is None


def test_mark_sent_wipes_the_payload_by_default(queue: QueueService, account: Account) -> None:
    job = _enqueue(queue, account)
    queue.claim_next(worker_id="w1", now=NOW, lease_seconds=60)
    queue.mark_sent(job_id=job.id, result=RESULT, now=NOW)
    stored = queue.get(job.id)
    assert stored is not None
    assert stored.raw_payload_enc is None


def test_mark_sent_can_keep_the_payload(
    queue: QueueService, account: Account, settings, cipher
) -> None:
    keeping = QueueService(
        session_factory=queue._session_factory,
        cipher=cipher,
        clock=queue._clock,
        settings=settings.model_copy(update={"keep_sent_payloads": True}),
        sleeper=queue._sleeper,
    )
    job = _enqueue(keeping, account)
    keeping.claim_next(worker_id="w1", now=NOW, lease_seconds=60)
    keeping.mark_sent(job_id=job.id, result=RESULT, now=NOW)
    stored = keeping.get(job.id)
    assert stored is not None and stored.raw_payload_enc is not None


def test_mark_sent_records_latency(queue: QueueService, account: Account, session_factory) -> None:
    job = _enqueue(queue, account)
    queue.claim_next(worker_id="w1", now=NOW, lease_seconds=60)
    queue.mark_sent(job_id=job.id, result=RESULT, now=NOW + timedelta(milliseconds=250))
    with session_factory() as session:
        event = session.scalar(
            select(SendEvent).where(SendEvent.job_id == job.id, SendEvent.event == "succeeded")
        )
    assert event is not None
    assert event.latency_ms == 250


def test_mark_failed_is_terminal(queue: QueueService, account: Account) -> None:
    job = _enqueue(queue, account)
    queue.claim_next(worker_id="w1", now=NOW, lease_seconds=60)
    queue.mark_failed(
        job_id=job.id, error_code="daily_send_quota_exceeded", message="nope", now=NOW
    )
    stored = queue.get(job.id)
    assert stored is not None
    assert stored.status == "failed"
    assert stored.error_code == "daily_send_quota_exceeded"
    assert stored.error_message == "nope"
    assert stored.finished_at == NOW
    assert stored.lease_expires_at is None


def test_reschedule_releases_the_lease_and_delays(queue: QueueService, account: Account) -> None:
    job = _enqueue(queue, account)
    queue.claim_next(worker_id="w1", now=NOW, lease_seconds=60)
    queue.reschedule(
        job_id=job.id, delay_seconds=30, error_code="rate_limit", message="slow down", now=NOW
    )
    stored = queue.get(job.id)
    assert stored is not None
    assert stored.status == "pending"
    assert stored.scheduled_at == NOW + timedelta(seconds=30)
    assert stored.lease_expires_at is None
    assert stored.worker_id is None
    assert stored.error_code == "rate_limit"


def test_rescheduled_job_is_not_claimed_before_its_time(
    queue: QueueService, account: Account
) -> None:
    job = _enqueue(queue, account)
    queue.claim_next(worker_id="w1", now=NOW, lease_seconds=60)
    queue.reschedule(job_id=job.id, delay_seconds=30, error_code="x", message="y", now=NOW)
    assert (
        queue.claim_next(worker_id="w1", now=NOW + timedelta(seconds=10), lease_seconds=60) is None
    )
    assert (
        queue.claim_next(worker_id="w1", now=NOW + timedelta(seconds=31), lease_seconds=60)
        is not None
    )


# ------------------------------------------------------------------- leases


def test_requeue_expired_leases_recovers_a_crashed_worker(
    queue: QueueService, account: Account
) -> None:
    _enqueue(queue, account)
    queue.claim_next(worker_id="dead-worker", now=NOW, lease_seconds=60)
    assert queue.requeue_expired_leases(now=NOW + timedelta(seconds=30)) == 0
    assert queue.requeue_expired_leases(now=NOW + timedelta(seconds=61)) == 1
    recovered = queue.claim_next(worker_id="w2", now=NOW + timedelta(seconds=61), lease_seconds=60)
    assert recovered is not None
    assert recovered.worker_id == "w2"


def test_requeue_records_an_event(queue: QueueService, account: Account, session_factory) -> None:
    job = _enqueue(queue, account)
    queue.claim_next(worker_id="dead", now=NOW, lease_seconds=60)
    queue.requeue_expired_leases(now=NOW + timedelta(seconds=61))
    with session_factory() as session:
        events = list(
            session.scalars(
                select(SendEvent).where(SendEvent.job_id == job.id).order_by(SendEvent.id)
            )
        )
    assert [e.event for e in events] == ["enqueued", "claimed", "requeued"]


# -------------------------------------------------------------------- depth


def test_depth_counts_pending_and_processing(queue: QueueService, account: Account) -> None:
    first = _enqueue(queue, account)
    _enqueue(queue, account)
    queue.claim_next(worker_id="w1", now=NOW, lease_seconds=60)
    assert queue.depth() == 2
    assert queue.depth(account_id=first.account_id) == 2


def test_depth_excludes_terminal_jobs(queue: QueueService, account: Account) -> None:
    job = _enqueue(queue, account)
    queue.claim_next(worker_id="w1", now=NOW, lease_seconds=60)
    queue.mark_sent(job_id=job.id, result=RESULT, now=NOW)
    assert queue.depth() == 0
