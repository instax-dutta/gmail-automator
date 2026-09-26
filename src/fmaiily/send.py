from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from fmaiily.accounts import AccountService
from fmaiily.api_keys import SCOPE_SEND, ApiKeyContext
from fmaiily.clock import Clock, Sleeper
from fmaiily.config import Settings
from fmaiily.errors import Forbidden, InvalidRequest
from fmaiily.gmail.mime import OutgoingMessage, build_mime, count_recipients
from fmaiily.models import Account, SendJob
from fmaiily.queue import QueueService
from fmaiily.quota import QuotaService, QuotaSnapshot

SendStatus = Literal["sent", "queued", "failed"]

#: Refuse a body larger than this before MIME construction, so an oversized request cannot
#: consume worker memory twice.
_ENCODE_CHUNK = 64 * 1024


@dataclass(frozen=True)
class SendOutcome:
    job_id: int
    status: SendStatus
    message_id: str | None
    error_code: str | None
    error_message: str | None
    account_email: str
    quota: QuotaSnapshot | None


@dataclass(frozen=True)
class BatchOutcome:
    outcomes: tuple[SendOutcome, ...]

    @property
    def sent(self) -> int:
        return sum(1 for o in self.outcomes if o.status == "sent")

    @property
    def failed(self) -> int:
        return sum(1 for o in self.outcomes if o.status == "failed")


class SendService:
    """Validates, quota-checks, builds MIME, and enqueues. It never calls Gmail itself.

    Ordering matters: everything that can refuse the request without side effects (validation,
    permissions, quota) happens *before* a job exists, so a rejected send leaves no trace in the
    queue and consumes no quota reservation. Only after that is a job created, and only the
    worker performs the actual API call.
    """

    settings: Settings

    def __init__(
        self,
        *,
        settings: Settings,
        accounts: AccountService,
        quota: QuotaService,
        queue: QueueService,
        clock: Clock,
        sleeper: Sleeper | None = None,
    ) -> None:
        self.settings = settings
        self._accounts = accounts
        self._quota = quota
        self._queue = queue
        self._clock = clock
        self._sleeper = sleeper

    def send(
        self,
        *,
        account_email: str | None,
        msg: OutgoingMessage,
        source: str,
        api_key: ApiKeyContext | None = None,
        idempotency_key: str | None = None,
        wait: bool = True,
        wait_timeout: float | None = None,
        now: datetime | None = None,
        thread_id: str | None = None,
    ) -> SendOutcome:
        now = now or self._clock.now()
        timeout = self.settings.send_wait_timeout_seconds if wait_timeout is None else wait_timeout

        if api_key is not None:
            api_key.require_scope(SCOPE_SEND)

        account = self._accounts.resolve(account_email)
        if api_key is not None:
            api_key.require_account(account.email)
        self._require_usable(account)

        recipients = count_recipients(msg)
        self._validate(msg, recipients=recipients)
        self._quota.check(account, recipients, now=now)

        payload = build_mime(msg, now=now)
        job = self._queue.enqueue(
            account=account,
            payload=payload,
            recipients=recipients,
            source=source,
            now=now,
            api_key_id=api_key.key_id if api_key else None,
            idempotency_key=idempotency_key,
            idempotency_scope=(f"key:{api_key.key_id}" if api_key and api_key.key_id else "global")
            if idempotency_key
            else None,
            thread_id=thread_id,
        )

        if not wait:
            return SendOutcome(
                job_id=job.id,
                status="queued",
                message_id=None,
                error_code=None,
                error_message=None,
                account_email=account.email,
                quota=self._quota.snapshot(account, now=now),
            )

        final = self._queue.wait_for_terminal(job.id, timeout=timeout, sleeper=self._sleeper)
        return self.outcome_for(final, account=account, fallback_job_id=job.id, now=now)

    def send_batch(
        self,
        *,
        account_email: str | None,
        messages: list[OutgoingMessage],
        source: str,
        api_key: ApiKeyContext | None = None,
        wait: bool = True,
        wait_timeout: float | None = None,
        now: datetime | None = None,
    ) -> BatchOutcome:
        """Enqueue several messages.

        A pre-flight refusal (validation, permissions, quota) propagates immediately and nothing
        is queued. Once jobs exist, per-job outcomes are returned rather than raised, so one bad
        recipient list cannot hide the fate of the rest of the batch.
        """
        if not messages:
            raise InvalidRequest("a batch must contain at least one message")

        account = self._accounts.resolve(account_email)
        if api_key is not None:
            api_key.require_account(account.email)
        self._require_usable(account)
        # Pre-flight the whole batch before creating any job, so a refusal halfway through cannot
        # leave a partially queued batch behind. Quota is checked against the running total of
        # recipients the batch would consume, not one message at a time.
        cumulative = 0
        for msg in messages:
            recipients = count_recipients(msg)
            self._validate(msg, recipients=recipients)
            cumulative += recipients
            self._quota.check(account, cumulative, now=now)

        outcomes: list[SendOutcome] = []
        for msg in messages:
            outcomes.append(
                self.send(
                    account_email=account_email,
                    msg=msg,
                    source=source,
                    api_key=api_key,
                    wait=wait,
                    wait_timeout=wait_timeout,
                    now=now,
                )
            )
        return BatchOutcome(outcomes=tuple(outcomes))

    # --------------------------------------------------------------- internals

    def _require_usable(self, account: Account) -> None:
        if account.status == "active":
            return
        raise Forbidden(
            f"account {account.email} is {account.status}; reconnect it before sending",
            details={"account": account.email, "status": account.status},
        )

    def _validate(self, msg: OutgoingMessage, *, recipients: int) -> None:
        settings = self.settings
        if recipients < 1:
            raise InvalidRequest(
                "a message needs at least one recipient", details={"account": msg.from_email}
            )
        if recipients > settings.max_recipients_per_message:
            raise InvalidRequest(
                f"a message may have at most {settings.max_recipients_per_message} recipients; "
                f"this one has {recipients}",
                details={
                    "recipients": recipients,
                    "max_recipients_per_message": settings.max_recipients_per_message,
                },
            )
        body_bytes = _utf8_len(msg.body) + _utf8_len(msg.body_html or "")
        if body_bytes > settings.max_body_bytes:
            raise InvalidRequest(
                f"message body is {body_bytes} bytes, over the "
                f"{settings.max_body_bytes} byte limit",
                details={"body_bytes": body_bytes, "max_body_bytes": settings.max_body_bytes},
            )
        if msg.attachments and not settings.attachments_enabled:
            raise InvalidRequest(
                "attachments are disabled; set FMAIILY_ATTACHMENTS_ENABLED=true to allow them",
                details={"attachments": len(msg.attachments)},
            )
        for attachment in msg.attachments:
            if len(attachment.content) > settings.attachment_max_bytes:
                from fmaiily.errors import AttachmentTooLarge

                raise AttachmentTooLarge(
                    f"attachment {attachment.filename} is {len(attachment.content)} bytes, over "
                    f"the {settings.attachment_max_bytes} byte limit",
                    details={
                        "filename": attachment.filename,
                        "bytes": len(attachment.content),
                        "max_bytes": settings.attachment_max_bytes,
                    },
                )

    def outcome_for(
        self,
        job: SendJob | None,
        *,
        account: Account,
        fallback_job_id: int,
        now: datetime | None = None,
    ) -> SendOutcome:
        """Map a persisted job row onto the agent-facing outcome shape."""
        now = now or self._clock.now()
        if job is None:
            return SendOutcome(
                job_id=fallback_job_id,
                status="failed",
                message_id=None,
                error_code="job_not_found",
                error_message="the send job disappeared while waiting for it",
                account_email=account.email,
                quota=None,
            )
        status = job.status
        job_id = job.id
        message_id = job.gmail_message_id
        error_code = job.error_code
        error_message = job.error_message
        if status == "sent":
            mapped: SendStatus = "sent"
        elif status in ("pending", "processing"):
            mapped = "queued"
        else:
            mapped = "failed"
        return SendOutcome(
            job_id=job_id,
            status=mapped,
            message_id=message_id,
            error_code=error_code,
            error_message=error_message,
            account_email=account.email,
            quota=self._quota.snapshot(account, now=now),
        )


def _utf8_len(value: str) -> int:
    total = 0
    for index in range(0, len(value), _ENCODE_CHUNK):
        total += len(value[index : index + _ENCODE_CHUNK].encode())
    return total


__all__ = ["BatchOutcome", "SendOutcome", "SendService", "SendStatus"]
