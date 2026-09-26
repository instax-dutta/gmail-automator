from __future__ import annotations

import random
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from fmaiily.accounts import AccountService
from fmaiily.clock import Sleeper
from fmaiily.container import Container, require
from fmaiily.gmail.client import AuthExpired, SendResult
from fmaiily.gmail.mime import to_raw_b64
from fmaiily.history import HistoryService
from fmaiily.logging_setup import get_logger
from fmaiily.metrics import Metrics
from fmaiily.models import Account, SendJob
from fmaiily.queue import QueueService
from fmaiily.quota import QuotaService
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
        maintenance_every: int = 50,
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
        # Optional: a worker built by hand (or by a test) simply reports no metrics.
        self._metrics: Metrics | None = getattr(container, "metrics", None)
        self._maintenance_every = max(1, maintenance_every)
        self._iterations = 0
        self._queue: QueueService = require(container, "queue")
        self._tokens: TokenManager = require(container, "tokens")

    def run_once(self, *, now: datetime | None = None) -> ProcessResult:
        now = now or self.container.clock.now()
        self._iterations += 1
        if self._iterations % self._maintenance_every == 0:
            self.run_maintenance(now=now)

        recovered = self._queue.requeue_expired_leases(now=now)
        if recovered:
            _log.info("recovered_expired_leases", count=recovered, worker_id=self.worker_id)

        job = self._queue.claim_next(
            worker_id=self.worker_id, now=now, lease_seconds=self.lease_seconds
        )
        if job is None:
            self._publish_gauges(now=now)
            return self._recorded(ProcessResult(action="idle"))

        return self._recorded(self._process(job, now=now))

    def run_maintenance(self, *, now: datetime | None = None) -> dict[str, int]:
        """Periodic housekeeping that no single send should pay for.

        Wipes queued payloads past their retention and deletes terminal history rows past the
        configured window, so neither grows without bound on a long-lived deployment.
        """
        now = now or self.container.clock.now()
        swept = self._queue.release_payloads(now=now)
        purged = 0
        history: HistoryService | None = getattr(self.container, "history", None)
        if history is not None:
            purged = history.purge_older_than(days=self.settings.history_retention_days, now=now)
        if swept or purged:
            _log.info(
                "maintenance",
                payloads_swept=swept,
                history_purged=purged,
                retention_days=self.settings.history_retention_days,
            )
        return {"payloads_swept": swept, "history_purged": purged}

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
        started = time.monotonic()

        try:
            account = self._account_for(job)
        except Exception as exc:
            return self._fail(job_id, exc, now=now, source=job.source, started=started)

        try:
            payload = self._queue.decrypt_payload(job)
        except Exception as exc:
            return self._fail(job_id, exc, now=now, source=job.source, started=started)

        try:
            access = self._tokens.access_token(account.email, now=now)
        except Exception as exc:
            self._note_token_refresh(account.email, "error")
            return self._fail(job_id, exc, now=now, source=job.source, started=started)
        self._note_token_refresh(account.email, "ok" if access.refreshed else "cached")

        try:
            result = self._send(
                email=account.email,
                access_token=access.token,
                payload=payload,
                thread_id=job.thread_id,
            )
        except AuthExpired:
            # A stale token is not a rate limit: refresh and try once more, immediately.
            self._tokens.invalidate(account.email)
            try:
                fresh = self._tokens.access_token(account.email, now=now)
                self._note_token_refresh(account.email, "ok")
                result = self._send(
                    email=account.email,
                    access_token=fresh.token,
                    payload=payload,
                    thread_id=job.thread_id,
                )
            except Exception as exc:
                return self._classify(
                    job_id,
                    exc,
                    attempt=job.attempt_count,
                    now=now,
                    source=job.source,
                    started=started,
                    account_email=account.email,
                )
        except Exception as exc:
            return self._classify(
                job_id,
                exc,
                attempt=job.attempt_count,
                now=now,
                source=job.source,
                started=started,
                account_email=account.email,
            )

        self._queue.mark_sent(job_id=job_id, result=result, now=now)
        elapsed = time.monotonic() - started
        _log.info(
            "send_succeeded",
            account=account.email,
            job_id=job_id,
            recipients=job.recipients,
            message_id=result.message_id,
            attempt=job.attempt_count,
            latency_ms=int(elapsed * 1000),
        )
        if self._metrics is not None:
            self._metrics.record_send(account.email, "sent", source=job.source)
            self._metrics.observe_send_duration(account.email, elapsed)
        self._publish_gauges(now=now)
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
        self,
        job_id: int,
        exc: BaseException,
        *,
        attempt: int,
        now: datetime,
        source: str = "unknown",
        started: float | None = None,
        account_email: str | None = None,
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
        return self._fail(
            job_id,
            exc,
            now=now,
            error_code=decision.error_code,
            source=source,
            started=started,
            account_email=account_email,
        )

    def _fail(
        self,
        job_id: int,
        exc: BaseException,
        *,
        now: datetime,
        error_code: str | None = None,
        source: str = "unknown",
        started: float | None = None,
        account_email: str | None = None,
    ) -> ProcessResult:
        from fmaiily.retry import classify

        code = error_code or classify(exc)
        message = str(getattr(exc, "message", None) or exc)
        self._queue.mark_failed(job_id=job_id, error_code=code, message=message, now=now)
        _log.warning("send_failed", job_id=job_id, error_code=code, reason=str(exc)[:200])
        label = account_email or _account_label(self.container, job_id)
        if self._metrics is not None and label is not None:
            self._metrics.record_send(label, "failed", source=source, error_code=code)
            if started is not None:
                self._metrics.observe_send_duration(label, time.monotonic() - started)
        return ProcessResult(action="failed", job_id=job_id, error_code=code)

    def _note_token_refresh(self, account: str, result: str) -> None:
        if self._metrics is not None:
            self._metrics.record_token_refresh(account, result)  # type: ignore[arg-type]

    def _publish_gauges(self, *, now: datetime) -> None:
        if self._metrics is None:
            return
        self._metrics.set_queue_depth(self._queue.depth())
        quota: QuotaService | None = getattr(self.container, "quota", None)
        accounts: AccountService | None = getattr(self.container, "accounts", None)
        if quota is None or accounts is None:
            return
        for account in accounts.list_active():
            snapshot = quota.snapshot(account, now=now)
            self._metrics.set_quota_remaining(
                account.email, "messages", snapshot.messages_remaining
            )
            self._metrics.set_quota_remaining(
                account.email, "recipients", snapshot.recipients_remaining
            )

    def _recorded(self, result: ProcessResult) -> ProcessResult:
        if self._metrics is not None:
            self._metrics.record_worker_iteration(result.action)
        return result


def _account_label(container: Container, job_id: int) -> str | None:
    """Best-effort account address for a metric label; metrics must never break a send."""
    try:
        queue: QueueService = require(container, "queue")
        job = queue.get(job_id)
        if job is None:
            return None
        accounts: AccountService = require(container, "accounts")
        return accounts.get_by_id(job.account_id).email
    except Exception:
        return None


def build_worker(container: Container, *, worker_id: str | None = None) -> Worker:
    return Worker(
        container,
        worker_id=worker_id
        or container.settings.worker_id
        or f"{container.settings.host}-{id(container) & 0xFFFF:04x}",
    )


__all__ = ["Action", "ProcessResult", "Worker", "build_worker"]
