from __future__ import annotations

import random
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from fmaiily.accounts import AccountService
from fmaiily.clock import Sleeper
from fmaiily.container import Container, require
from fmaiily.gmail.client import AuthExpired, SendResult
from fmaiily.gmail.mime import to_raw_b64
from fmaiily.logging_setup import get_logger
from fmaiily.models import Account, SendJob
from fmaiily.queue import QueueService
from fmaiily.retry import decide
from fmaiily.tokens import TokenManager

Action = Literal["idle", "sent", "retry_scheduled", "failed", "requeued"]

_log = get_logger("fmaiily.worker")


@dataclass(frozen=True)
class ProcessResult:
    action: Action
    job_id: int | None = None
    message_id: str | None = None
    error_code: str | None = None


class Worker:
    """Drains the send queue: claim, refresh, send, classify, retry or finish.

    `run_once` is the whole worker in one deterministic step so tests can drive it directly with
    an injected clock; `run_forever` is a thin loop around it. A single send failure never
    propagates out of `run_once` - it is classified, recorded on the job, and returned.
    """

    def __init__(
        self,
        container: Container,
        *,
        worker_id: str,
        poll_interval: float | None = None,
        lease_seconds: int | None = None,
        sleeper: Sleeper | None = None,
        rand: Callable[[], float] | None = None,
    ) -> None:
        self.container = container
        self.worker_id = worker_id
        self.settings = container.settings
        self.poll_interval = (
            poll_interval
            if poll_interval is not None
            else container.settings.worker_poll_interval_seconds
        )
        self.lease_seconds = (
            lease_seconds if lease_seconds is not None else container.settings.worker_lease_seconds
        )
        self._sleeper = sleeper or container.sleeper
        self._rand: Callable[[], float] = rand or random.random
        self._queue: QueueService = require(container, "queue")
        self._tokens: TokenManager = require(container, "tokens")

    def run_once(self, *, now: datetime | None = None) -> ProcessResult:
        now = now or self.container.clock.now()
        recovered = self._queue.requeue_expired_leases(now=now)
        if recovered:
            _log.info("recovered_expired_leases", count=recovered, worker_id=self.worker_id)

        job = self._queue.claim_next(
            worker_id=self.worker_id, now=now, lease_seconds=self.lease_seconds
        )
        if job is None:
            return ProcessResult(action="idle")

        return self._process(job, now=now)

    def run_forever(self, stop_event: threading.Event) -> None:
        while not stop_event.is_set():
            result = self.run_once()
            if result.action != "idle":
                continue
            if self._sleeper is not None:
                self._sleeper.sleep(self.poll_interval)
            else:
                stop_event.wait(self.poll_interval)

    # --------------------------------------------------------------- internals

    def _process(self, job: SendJob, *, now: datetime) -> ProcessResult:
        job_id = job.id

        try:
            account = self._account_for(job)
        except Exception as exc:
            return self._fail(job_id, exc, now=now)

        try:
            payload = self._queue.decrypt_payload(job)
        except Exception as exc:
            return self._fail(job_id, exc, now=now)

        try:
            token = self._tokens.access_token(account.email, now=now)
        except Exception as exc:
            return self._fail(job_id, exc, now=now)

        try:
            result = self._send(
                email=account.email,
                access_token=token.token,
                payload=payload,
                thread_id=job.thread_id,
            )
        except AuthExpired:
            # A stale token is not a rate limit: refresh and try once more, immediately.
            self._tokens.invalidate(account.email)
            try:
                fresh = self._tokens.access_token(account.email, now=now)
                result = self._send(
                    email=account.email,
                    access_token=fresh.token,
                    payload=payload,
                    thread_id=job.thread_id,
                )
            except Exception as exc:
                return self._classify(job_id, exc, attempt=job.attempt_count, now=now)
        except Exception as exc:
            return self._classify(job_id, exc, attempt=job.attempt_count, now=now)

        self._queue.mark_sent(job_id=job_id, result=result, now=now)
        _log.info(
            "send_succeeded",
            account=account.email,
            job_id=job_id,
            recipients=job.recipients,
            message_id=result.message_id,
            attempt=job.attempt_count,
        )
        return ProcessResult(action="sent", job_id=job_id, message_id=result.message_id)

    def _send(
        self, *, email: str, access_token: str, payload: bytes, thread_id: str | None
    ) -> SendResult:
        return self.container.transport.send_raw(
            email=email,
            access_token=access_token,
            raw_b64url=to_raw_b64(payload),
            thread_id=thread_id,
        )

    def _account_for(self, job: SendJob) -> Account:
        from fmaiily.errors import SendFailed

        accounts: AccountService = require(self.container, "accounts")
        try:
            account = accounts.get_by_id(job.account_id)
        except Exception as exc:
            raise SendFailed(
                f"send job {job.id} references account {job.account_id}, which no longer exists"
            ) from exc
        if account.status != "active":
            raise SendFailed(
                f"account {account.email} is {account.status}",
                details={"account": account.email, "status": account.status},
            )
        return account

    def _classify(
        self, job_id: int, exc: BaseException, *, attempt: int, now: datetime
    ) -> ProcessResult:
        decision = decide(
            exc,
            attempt=attempt,
            max_attempts=self.settings.max_attempts,
            base=self.settings.backoff_base_seconds,
            cap=self.settings.backoff_max_seconds,
            rand=self._rand,
        )
        if decision.action == "retry":
            self._queue.reschedule(
                job_id=job_id,
                delay_seconds=decision.delay_seconds,
                error_code=decision.error_code,
                message=decision.message,
                now=now,
            )
            _log.warning(
                "send_retry_scheduled",
                job_id=job_id,
                attempt=attempt,
                delay_seconds=decision.delay_seconds,
                error_code=decision.error_code,
                reason=decision.reason,
            )
            return ProcessResult(
                action="retry_scheduled", job_id=job_id, error_code=decision.error_code
            )
        return self._fail(job_id, exc, now=now, error_code=decision.error_code)

    def _fail(
        self,
        job_id: int,
        exc: BaseException,
        *,
        now: datetime,
        error_code: str | None = None,
    ) -> ProcessResult:
        from fmaiily.retry import classify

        code = error_code or classify(exc)
        message = str(getattr(exc, "message", None) or exc)
        self._queue.mark_failed(job_id=job_id, error_code=code, message=message, now=now)
        _log.warning("send_failed", job_id=job_id, error_code=code, reason=str(exc)[:200])
        return ProcessResult(action="failed", job_id=job_id, error_code=code)


def build_worker(container: Container, *, worker_id: str | None = None) -> Worker:
    return Worker(
        container,
        worker_id=worker_id
        or container.settings.worker_id
        or f"{container.settings.host}-{id(container) & 0xFFFF:04x}",
    )


__all__ = ["Action", "ProcessResult", "Worker", "build_worker"]
