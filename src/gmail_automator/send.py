from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal

from gmail_automator.accounts import AccountService
from gmail_automator.api_keys import SCOPE_SEND, ApiKeyContext
from gmail_automator.clock import Clock, Sleeper
from gmail_automator.config import Settings
from gmail_automator.errors import Forbidden, InvalidRequest
from gmail_automator.gmail.mime import OutgoingMessage, build_mime, count_recipients
from gmail_automator.models import Account, SendJob
from gmail_automator.queue import QueueService
from gmail_automator.quota import QuotaService, QuotaSnapshot

if TYPE_CHECKING:
    from gmail_automator.schemas import SendEmailRequest

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
        self.accounts = accounts
        self.quota = quota
        self.queue = queue
        self.clock = clock
        self._sleeper = sleeper

    def send(
        self,
        *,
        account_email: str | None,
        msg: OutgoingMessage,
        source: str,
        api_key: ApiKeyContext | None = None,
        idempotency_key: str | None = None,
        request_hash: str | None = None,
        wait: bool = True,
        wait_timeout: float | None = None,
        now: datetime | None = None,
        thread_id: str | None = None,
    ) -> SendOutcome:
        now = now or self.clock.now()
        timeout = self.settings.send_wait_timeout_seconds if wait_timeout is None else wait_timeout

        if api_key is not None:
            api_key.require_scope(SCOPE_SEND)

        account = self.accounts.resolve(account_email)
        if api_key is not None:
            api_key.require_account(account.email)
        self._require_usable(account)

        recipients = count_recipients(msg)
        self._validate(msg, recipients=recipients)
        self.quota.check(account, recipients, now=now)

        payload = build_mime(msg, now=now)
        scope = (
            f"key:{api_key.key_id}"
            if api_key is not None and api_key.key_id is not None
            else "global"
        )
        job = self.queue.enqueue(
            account=account,
            payload=payload,
            recipients=recipients,
            source=source,
            now=now,
            api_key_id=api_key.key_id if api_key else None,
            idempotency_key=idempotency_key,
            idempotency_scope=scope,
            request_hash=request_hash,
            thread_id=thread_id,
        )

        if wait:
            final = self.queue.wait_for_terminal(job.id, timeout=timeout, sleeper=self._sleeper)
        else:
            # A replay can land on a job that is already terminal, so the real state is reported
            # rather than assuming "queued".
            final = job
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

        account = self.accounts.resolve(account_email)
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
            self.quota.check(account, cumulative, now=now)

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
                "attachments are disabled; set GMAIL_AUTOMATOR_ATTACHMENTS_ENABLED=true "
                "to allow them",
                details={"attachments": len(msg.attachments)},
            )
        # Re-check the cap here even for inline content: by this point the bytes are already
        # decoded and in memory, and the cap is a gateway policy rather than a schema rule.
        for attachment in msg.attachments:
            if len(attachment.content) > settings.attachment_max_bytes:
                from gmail_automator.errors import AttachmentTooLarge

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
        now = now or self.clock.now()
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
            quota=self.quota.snapshot(account, now=now),
        )


def request_fingerprint(request: SendEmailRequest) -> str:
    """Stable digest of everything that determines what is sent.

    Used to tell an idempotent retry (same intent, replay the original job) from a genuine reuse
    of the key for a different message. `thread_id` is excluded because it is transport metadata,
    not part of the message, and it defaults to null so its absence must not change the digest.
    """
    payload: dict[str, Any] = {
        "to": list(request.to),
        "cc": list(request.cc),
        "bcc": list(request.bcc),
        "subject": request.subject,
        "body": request.body,
        "body_html": request.body_html,
        "reply_to": request.reply_to,
        "in_reply_to": request.in_reply_to,
        "references": request.references,
        "attachments": [
            {
                "filename": item.filename,
                "mime_type": item.mime_type,
                "sha256": _digest(item.content_base64 or ""),
            }
            for item in request.attachments
        ],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode()).hexdigest()


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _utf8_len(value: str) -> int:
    total = 0
    for index in range(0, len(value), _ENCODE_CHUNK):
        total += len(value[index : index + _ENCODE_CHUNK].encode())
    return total


__all__ = ["BatchOutcome", "SendOutcome", "SendService", "SendStatus"]
