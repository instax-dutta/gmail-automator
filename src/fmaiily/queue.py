from __future__ import annotations

import base64
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, sessionmaker

from fmaiily.clock import Clock, Sleeper
from fmaiily.config import Settings
from fmaiily.crypto import TokenCipher
from fmaiily.errors import DuplicateRequest, QueueFull
from fmaiily.gmail.client import SendResult
from fmaiily.models import Account, SendEvent, SendJob

#: Job lifecycle. `pending` and `processing` hold a quota reservation; the rest are terminal.
PENDING = "pending"
PROCESSING = "processing"
SENT = "sent"
FAILED = "failed"
REJECTED = "rejected"
TERMINAL_STATUSES = (SENT, FAILED, REJECTED)
ACTIVE_STATUSES = (PENDING, PROCESSING)


class QueueService:
    """Durable send queue backed by `send_jobs` (master plan R5).

    Three properties matter and are each covered by tests:

    * **Durability** - the RFC 5322 payload is stored encrypted, so an in-flight send survives a
      restart. It is bound to the job id via AAD, so a payload cannot be moved between rows.
    * **Pacing by scheduling, not sleeping** - `enqueue` places the job at
      `max(now, account.next_send_at)` and pushes the cursor forward. A worker thread therefore
      never blocks on a per-account delay, and the pacing survives restarts and extra workers.
    * **Single-statement claim** - one `UPDATE ... WHERE id = (SELECT ... LIMIT 1) RETURNING id`
      gives a correct hand-off between concurrent workers on both SQLite 3.35+ and Postgres.
    """

    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        cipher: TokenCipher,
        clock: Clock,
        settings: Settings,
        sleeper: Sleeper | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._cipher = cipher
        self._clock = clock
        self._settings = settings
        self._sleeper = sleeper

    # ----------------------------------------------------------------- enqueue

    def enqueue(
        self,
        *,
        account: Account,
        payload: bytes,
        recipients: int,
        source: str,
        now: datetime | None = None,
        api_key_id: int | None = None,
        idempotency_key: str | None = None,
        idempotency_scope: str | None = None,
        thread_id: str | None = None,
    ) -> SendJob:
        now = now or self._clock.now()
        with self._session_factory() as session:
            if idempotency_key:
                existing = session.scalar(
                    select(SendJob).where(
                        SendJob.idempotency_scope == (idempotency_scope or "global"),
                        SendJob.idempotency_key == idempotency_key,
                    )
                )
                if existing is not None:
                    raise DuplicateRequest(
                        "an identical request was already submitted",
                        details={"job_id": existing.id, "idempotency_key": idempotency_key},
                    )

            if self._depth(session, account_id=account.id) >= self._settings.queue_max_depth:
                raise QueueFull(
                    f"send queue for {account.email} is full "
                    f"({self._settings.queue_max_depth} jobs); retry later",
                    details={
                        "account": account.email,
                        "queue_depth": self._settings.queue_max_depth,
                        "max_queue_depth": self._settings.queue_max_depth,
                    },
                )

            row = session.get(Account, account.id)
            if row is None:
                raise QueueFull(
                    f"account {account.email} disappeared before enqueue",
                    details={"account": account.email},
                )

            scheduled_at = max(now, row.next_send_at) if row.next_send_at else now
            horizon = now + timedelta(hours=self._settings.max_schedule_horizon_hours)
            if scheduled_at > horizon:
                raise QueueFull(
                    f"send queue for {account.email} is scheduled beyond "
                    f"{self._settings.max_schedule_horizon_hours}h; retry later",
                    details={
                        "account": account.email,
                        "scheduled_at": scheduled_at.isoformat(),
                        "horizon_hours": self._settings.max_schedule_horizon_hours,
                    },
                )
            row.next_send_at = scheduled_at + timedelta(seconds=row.send_interval_seconds)
            row.updated_at = now

            job = SendJob(
                account_id=account.id,
                status=PENDING,
                recipients=recipients,
                source=source,
                api_key_id=api_key_id,
                thread_id=thread_id,
                idempotency_scope=idempotency_scope,
                idempotency_key=idempotency_key,
                max_attempts=self._settings.max_attempts,
                scheduled_at=scheduled_at,
                payload_expires_at=now + timedelta(hours=self._settings.payload_retention_hours),
                created_at=now,
                updated_at=now,
            )
            session.add(job)
            session.flush()  # assign the id so the payload can be bound to it via AAD
            job.raw_payload_enc = self._cipher.encrypt(
                base64.urlsafe_b64encode(payload).decode(), aad=str(job.id)
            )
            self._event(session, job, "enqueued", now=now)
            session.commit()
            session.refresh(job)
            session.expunge(job)
            return job

    # ------------------------------------------------------------------- claim

    def claim_next(
        self, *, worker_id: str, now: datetime | None = None, lease_seconds: int = 120
    ) -> SendJob | None:
        now = now or self._clock.now()
        lease_until = now + timedelta(seconds=lease_seconds)
        with self._session_factory() as session:
            candidate = session.scalar(
                select(SendJob.id)
                .where(SendJob.status == PENDING, SendJob.scheduled_at <= now)
                .order_by(SendJob.scheduled_at, SendJob.id)
                .limit(1)
            )
            if candidate is None:
                return None
            claimed_id = session.scalar(
                update(SendJob)
                .where(
                    SendJob.id == candidate,
                    SendJob.status == PENDING,
                    SendJob.scheduled_at <= now,
                )
                .values(
                    status=PROCESSING,
                    lease_expires_at=lease_until,
                    worker_id=worker_id,
                    attempt_count=SendJob.attempt_count + 1,
                    updated_at=now,
                )
                .returning(SendJob.id)
            )
            if claimed_id is None:  # another worker won the race
                return None
            job = session.get(SendJob, claimed_id, populate_existing=True)
            assert job is not None
            self._event(session, job, "claimed", now=now, attempt=job.attempt_count)
            session.commit()
            session.expunge(job)
            return job

    # ---------------------------------------------------------------- outcomes

    def mark_sent(self, *, job_id: int, result: SendResult, now: datetime | None = None) -> None:
        now = now or self._clock.now()
        with self._session_factory() as session:
            job = self._require(session, job_id)
            latency_ms = max(0, int((now - job.created_at).total_seconds() * 1000))
            job.status = SENT
            job.gmail_message_id = result.message_id
            job.gmail_thread_id = result.thread_id
            job.sent_at = now
            job.finished_at = now
            job.error_code = None
            job.error_message = None
            job.worker_id = None
            job.lease_expires_at = None
            job.updated_at = now
            if not self._settings.keep_sent_payloads:
                job.raw_payload_enc = None
            self._event(
                session, job, "succeeded", now=now, attempt=job.attempt_count, latency_ms=latency_ms
            )
            session.commit()

    def mark_failed(
        self, *, job_id: int, error_code: str, message: str, now: datetime | None = None
    ) -> None:
        now = now or self._clock.now()
        with self._session_factory() as session:
            job = self._require(session, job_id)
            job.status = FAILED
            job.error_code = error_code
            job.error_message = message[:2000]
            job.finished_at = now
            job.worker_id = None
            job.lease_expires_at = None
            job.updated_at = now
            self._event(
                session,
                job,
                "failed",
                now=now,
                attempt=job.attempt_count,
                error_code=error_code,
                error_message=message[:2000],
            )
            session.commit()

    def reschedule(
        self,
        *,
        job_id: int,
        delay_seconds: float,
        error_code: str,
        message: str,
        now: datetime | None = None,
    ) -> None:
        now = now or self._clock.now()
        with self._session_factory() as session:
            job = self._require(session, job_id)
            job.status = PENDING
            job.scheduled_at = now + timedelta(seconds=delay_seconds)
            job.error_code = error_code
            job.error_message = message[:2000]
            job.worker_id = None
            job.lease_expires_at = None
            job.updated_at = now
            self._event(
                session,
                job,
                "retry_scheduled",
                now=now,
                attempt=job.attempt_count,
                error_code=error_code,
                error_message=message[:2000],
            )
            session.commit()

    def requeue_expired_leases(self, *, now: datetime | None = None) -> int:
        now = now or self._clock.now()
        with self._session_factory() as session:
            stale = list(
                session.scalars(
                    select(SendJob).where(
                        SendJob.status == PROCESSING,
                        SendJob.lease_expires_at.is_not(None),
                        SendJob.lease_expires_at < now,
                    )
                )
            )
            for job in stale:
                job.status = PENDING
                job.worker_id = None
                job.lease_expires_at = None
                job.updated_at = now
                self._event(session, job, "requeued", now=now, attempt=job.attempt_count)
            session.commit()
            return len(stale)

    def release_payloads(self, *, now: datetime | None = None) -> int:
        """Wipe payloads past their retention. Bodies are not kept longer than needed."""
        now = now or self._clock.now()
        with self._session_factory() as session:
            expired = list(
                session.scalars(
                    select(SendJob).where(
                        SendJob.raw_payload_enc.is_not(None),
                        SendJob.payload_expires_at.is_not(None),
                        SendJob.payload_expires_at < now,
                        SendJob.status.in_(TERMINAL_STATUSES),
                    )
                )
            )
            for job in expired:
                job.raw_payload_enc = None
                job.updated_at = now
            session.commit()
            return len(expired)

    # ------------------------------------------------------------------- reads

    def get(self, job_id: int) -> SendJob | None:
        with self._session_factory() as session:
            job = session.get(SendJob, job_id)
            if job is not None:
                session.expunge(job)
            return job

    def find_by_idempotency(self, *, scope: str, key: str) -> SendJob | None:
        with self._session_factory() as session:
            job = session.scalar(
                select(SendJob).where(
                    SendJob.idempotency_scope == scope, SendJob.idempotency_key == key
                )
            )
            if job is not None:
                session.expunge(job)
            return job

    def depth(self, *, account_id: int | None = None) -> int:
        with self._session_factory() as session:
            return self._depth(session, account_id=account_id)

    def decrypt_payload(self, job: SendJob) -> bytes:
        if not job.raw_payload_enc:
            raise ValueError(f"job {job.id} has no stored payload")
        raw_b64 = self._cipher.decrypt(job.raw_payload_enc, aad=str(job.id))
        return base64.urlsafe_b64decode(raw_b64 + "=" * (-len(raw_b64) % 4))

    def wait_for_terminal(
        self, job_id: int, *, timeout: float, sleeper: Sleeper | None = None
    ) -> SendJob | None:
        """Block until the job leaves the active states, or the timeout expires.

        Polling rather than condition-variable signalling keeps this usable from both the
        FastAPI threadpool and the MCP thread bridge with no shared event objects.
        """
        wait_on = sleeper or self._sleeper
        interval = self._settings.poll_interval_seconds
        waited = 0.0
        while True:
            job = self.get(job_id)
            if job is None or job.status not in ACTIVE_STATUSES:
                return job
            if waited >= timeout:
                return job
            if wait_on is not None:
                wait_on.sleep(interval)
            else:
                import time

                time.sleep(interval)
            waited += interval

    # --------------------------------------------------------------- internals

    def _depth(self, session: Session, *, account_id: int | None) -> int:
        query = select(func.count(SendJob.id)).where(SendJob.status.in_(ACTIVE_STATUSES))
        if account_id is not None:
            query = query.where(SendJob.account_id == account_id)
        return int(session.scalar(query) or 0)

    def _require(self, session: Session, job_id: int) -> SendJob:
        job = session.get(SendJob, job_id)
        if job is None:
            raise LookupError(f"send job {job_id} not found")
        return job

    def _event(
        self,
        session: Session,
        job: SendJob,
        event: str,
        *,
        now: datetime,
        attempt: int = 0,
        error_code: str | None = None,
        error_message: str | None = None,
        latency_ms: int | None = None,
    ) -> None:
        row: dict[str, Any] = {
            "job_id": job.id,
            "account_id": job.account_id,
            "event": event,
            "attempt": attempt,
            "error_code": error_code,
            "error_message": error_message,
            "latency_ms": latency_ms,
            "created_at": now,
        }
        session.add(SendEvent(**row))


__all__ = [
    "ACTIVE_STATUSES",
    "FAILED",
    "PENDING",
    "PROCESSING",
    "REJECTED",
    "SENT",
    "TERMINAL_STATUSES",
    "QueueService",
]
