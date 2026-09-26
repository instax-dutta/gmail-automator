"""Postgres support (opt-in, `uv sync --extra postgres`).

The default suite runs entirely on SQLite. These tests only run when `FMAIILY_TEST_POSTGRES_URL`
points at a reachable database, so a checkout without Postgres still gets a green suite.

What actually differs between the two backends, and is therefore worth testing here:

* `UPDATE ... RETURNING` claim semantics under real row locking (SQLite serializes writers with a
  file lock; Postgres uses `READ COMMITTED` plus row locks).
* Concurrent enqueues and claims from several threads without losing or double-claiming a job.
* `UTCDateTime` round-tripping through a real timestamp column.
"""

from __future__ import annotations

import base64
import os
import threading
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Engine, text

from fmaiily.config import Settings
from fmaiily.crypto import TokenCipher
from fmaiily.db import create_db_engine, create_session_factory, run_migrations
from fmaiily.gmail.client import SendResult
from fmaiily.gmail.mime import OutgoingMessage, build_mime, to_raw_b64
from fmaiily.models import Account, SendJob
from fmaiily.queue import QueueService

POSTGRES_URL = os.environ.get("FMAIILY_TEST_POSTGRES_URL")
pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not POSTGRES_URL, reason="set FMAIILY_TEST_POSTGRES_URL to run"),
]

NOW = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
FAKE_KEY = base64.urlsafe_b64encode(b"p" * 32).decode()


@pytest.fixture
def pg_settings() -> Settings:
    return Settings(
        token_encryption_key=FAKE_KEY,
        database_url=POSTGRES_URL or "",
        worker_enabled=False,
        auth_mode="none",
        _env_file=None,
    )


def _reset(engine: Engine) -> None:
    """Drop every table *including* the Alembic version stamp.

    `Base.metadata.drop_all` only knows the models, so it leaves `alembic_version` stamped at head
    and the next `upgrade head` becomes a silent no-op - which looks exactly like a broken
    migration. The stamp has to go with the schema.
    """
    from fmaiily.db import Base

    Base.metadata.drop_all(engine)
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS alembic_version"))


@pytest.fixture
def pg_engine(pg_settings) -> Engine:
    engine = create_db_engine(pg_settings.database_url)
    _reset(engine)
    run_migrations(pg_settings.database_url, pg_settings.validate_migrations())
    yield engine
    _reset(engine)
    engine.dispose()


@pytest.fixture
def pg_session_factory(pg_engine):
    return create_session_factory(pg_engine)


@pytest.fixture
def cipher(pg_settings) -> TokenCipher:
    return TokenCipher(pg_settings.encryption_key_bytes())


@pytest.fixture
def queue(pg_settings, pg_session_factory, cipher) -> QueueService:
    return QueueService(
        session_factory=pg_session_factory,
        cipher=cipher,
        clock=type("C", (), {"now": staticmethod(lambda: NOW)})(),
        settings=pg_settings,
    )


@pytest.fixture
def account(pg_session_factory) -> Account:
    with pg_session_factory() as session:
        row = Account(
            email="pg@example.com",
            token_uri="https://oauth2.googleapis.com/token",
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        session.expunge(row)
        return row


def _payload(account: Account) -> bytes:
    return build_mime(
        OutgoingMessage(from_email=account.email, to=("a@example.com",), subject="s", body="b"),
        now=NOW,
    )


def _enqueue(queue: QueueService, account: Account, count: int, **kwargs) -> list[int]:
    """Enqueue `count` jobs. Pacing spaces them 2s apart, so a caller that wants every job due at
    once must claim with an advancing clock - exactly as the worker does in production."""
    ids = []
    for _ in range(count):
        job = queue.enqueue(
            account=account,
            payload=_payload(account),
            recipients=1,
            source="api",
            now=NOW,
            **kwargs,
        )
        ids.append(job.id)
    return ids


def _pacing_times(queue: QueueService, ids: list[int]) -> list[datetime]:
    return [job.scheduled_at for job in (queue.get(job_id) for job_id in ids) if job is not None]


def test_migrations_apply_to_postgres(pg_engine) -> None:
    from sqlalchemy import inspect

    tables = set(inspect(pg_engine).get_table_names())
    assert {"accounts", "send_jobs", "send_events", "api_keys", "oauth_states"} <= tables
    with pg_engine.connect() as conn:
        assert conn.execute(text("select version_num from alembic_version")).scalar() is not None


def test_utc_datetime_round_trip(pg_session_factory) -> None:
    later = NOW + timedelta(hours=3)
    with pg_session_factory() as session:
        row = Account(
            email="tz@example.com",
            token_uri="https://oauth2.googleapis.com/token",
            created_at=later,
            updated_at=later,
        )
        session.add(row)
        session.commit()
    with pg_session_factory() as session:
        loaded = session.query(Account).filter_by(email="tz@example.com").one()
    assert loaded.created_at == later
    assert loaded.created_at.tzinfo is not None


def test_enqueue_and_claim_on_postgres(queue, account) -> None:
    job_id = _enqueue(queue, account, 1)[0]
    claimed = queue.claim_next(worker_id="w1", now=NOW, lease_seconds=60)
    assert claimed is not None
    assert claimed.id == job_id
    assert claimed.status == "processing"
    assert claimed.lease_expires_at == NOW + timedelta(seconds=60)


def test_pacing_spaces_jobs_just_like_sqlite(queue, account) -> None:
    ids = _enqueue(queue, account, 3)
    stored = [queue.get(job_id) for job_id in ids]
    assert [job.scheduled_at for job in stored if job] == [
        NOW,
        NOW + timedelta(seconds=2),
        NOW + timedelta(seconds=4),
    ]


def test_returning_claim_prevents_a_double_claim(queue, account) -> None:
    ids = _enqueue(queue, account, 2)
    first = queue.claim_next(worker_id="w1", now=NOW, lease_seconds=60)
    second = queue.claim_next(worker_id="w2", now=NOW + timedelta(seconds=2), lease_seconds=60)
    assert first is not None and second is not None
    assert first.id != second.id
    assert {first.id, second.id} == set(ids)


def test_concurrent_claims_never_hand_out_the_same_job(queue, account) -> None:
    _enqueue(queue, account, 8)
    # every job is due at once here: the claim query re-checks status, so a race is a no-op
    claimed: list[int | None] = []
    lock = threading.Lock()

    def drain(worker_id: str) -> None:
        while True:
            job = queue.claim_next(
                worker_id=worker_id, now=NOW + timedelta(hours=1), lease_seconds=600
            )
            with lock:
                claimed.append(job.id if job else None)
            if job is None:
                return

    threads = [threading.Thread(target=drain, args=(f"w{i}",)) for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    ids = [job_id for job_id in claimed if job_id is not None]
    assert len(ids) == 8
    assert len(set(ids)) == 8


def test_concurrent_enqueue_keeps_every_job(queue, account) -> None:
    created: list[int] = []
    lock = threading.Lock()

    def submit() -> None:
        job_id = _enqueue(queue, account, 1)[0]
        with lock:
            created.append(job_id)

    threads = [threading.Thread(target=submit) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(set(created)) == 6
    assert queue.depth() == 6


def test_mark_sent_persists_the_gmail_ids(queue, account) -> None:
    job_id = _enqueue(queue, account, 1)[0]
    queue.claim_next(worker_id="w1", now=NOW, lease_seconds=60)
    queue.mark_sent(
        job_id=job_id,
        result=SendResult(message_id="pg-1", thread_id="t", label_ids=("SENT",)),
        now=NOW,
    )
    stored = queue.get(job_id)
    assert stored is not None
    assert stored.status == "sent"
    assert stored.gmail_message_id == "pg-1"
    assert stored.raw_payload_enc is None


def test_lease_recovery_on_postgres(queue, account) -> None:
    job_id = _enqueue(queue, account, 1)[0]
    queue.claim_next(worker_id="dead", now=NOW, lease_seconds=10)
    assert queue.requeue_expired_leases(now=NOW + timedelta(seconds=5)) == 0
    assert queue.requeue_expired_leases(now=NOW + timedelta(seconds=30)) == 1
    recovered = queue.claim_next(worker_id="w2", now=NOW + timedelta(seconds=30), lease_seconds=60)
    assert recovered is not None and recovered.id == job_id


def test_idempotency_unique_constraint_holds_on_postgres(queue, account) -> None:
    """The database, not just the application, refuses a second row for the same key.

    `QueueService.enqueue` is called directly here, so the scope has to be supplied explicitly -
    `SendService` is what normally fills it in.
    """
    from sqlalchemy.exc import IntegrityError

    first = _enqueue(queue, account, 1, idempotency_key="idem-1", idempotency_scope="global")[0]
    stored = queue.get(first)
    assert stored is not None
    with pytest.raises(IntegrityError), queue.session_factory() as session:
        session.add(
            SendJob(
                account_id=account.id,
                status="pending",
                recipients=1,
                source="api",
                scheduled_at=NOW,
                created_at=NOW,
                updated_at=NOW,
                idempotency_scope=stored.idempotency_scope,
                idempotency_key=stored.idempotency_key,
            )
        )
        session.commit()


def test_payload_encryption_round_trips_on_postgres(queue, account, cipher) -> None:
    job_id = _enqueue(queue, account, 1)[0]
    stored = queue.get(job_id)
    assert stored is not None and stored.raw_payload_enc is not None
    raw_b64 = cipher.decrypt(stored.raw_payload_enc, aad=str(job_id))
    assert b"Subject: s" in base64.urlsafe_b64decode(raw_b64 + "===")
    assert to_raw_b64(b"x")  # keeps the import meaningful
