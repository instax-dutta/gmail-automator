from __future__ import annotations

import base64
import binascii
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, EmailStr, Field, ValidationError, model_validator

from gmail_automator.attachments import resolve_attachments
from gmail_automator.config import Settings
from gmail_automator.gmail.mime import Attachment, OutgoingMessage

SCHEMA_VERSION = "v1"


class AttachmentIn(BaseModel):
    filename: str = Field(min_length=1, max_length=255)
    content_base64: str | None = None
    path: str | None = None
    #: Empty means "guess from the filename, else application/octet-stream". An explicit value is
    #: always honoured, so a caller can force a type the guesser would get wrong.
    mime_type: str = ""

    @model_validator(mode="after")
    def _exactly_one_source(self) -> AttachmentIn:
        if (self.content_base64 is None) == (self.path is None):
            raise ValueError("provide exactly one of content_base64 or path")
        return self


class SendEmailRequest(BaseModel):
    account: str | None = None
    to: list[EmailStr] = Field(min_length=1)
    subject: str = Field(max_length=998)
    body: str
    body_html: str | None = None
    cc: list[EmailStr] = []
    bcc: list[EmailStr] = []
    reply_to: EmailStr | None = None
    in_reply_to: str | None = None
    references: str | None = None
    thread_id: str | None = None
    attachments: list[AttachmentIn] = []
    idempotency_key: str | None = Field(default=None, max_length=120)
    wait: bool = True


class BatchSendRequest(BaseModel):
    account: str | None = None
    emails: list[SendEmailRequest] = Field(min_length=1, max_length=50)
    #: One decision for the whole batch: a per-message `wait` inside `emails` is not honored,
    #: because pacing means the messages complete at different times anyway. Defaults to False so
    #: a bulk submit returns immediately with job ids.
    wait: bool = False


class SendEmailResponse(BaseModel):
    job_id: int
    status: Literal["sent", "queued", "failed"]
    message_id: str | None = None
    account: str
    error_code: str | None = None
    error_message: str | None = None


class BatchSendResponse(BaseModel):
    jobs: list[SendEmailResponse]


class JobStatusResponse(BaseModel):
    job_id: int
    status: str
    account: str
    recipients: int
    attempts: int
    #: Gmail's message id, which is what `reply` takes as `message_id`. Not the RFC Message-ID:
    #: the reply tool reads the headers to get that, because the two are different values.
    message_id: str | None = None
    #: Gmail's thread id. Passing it back on a send or reply is what makes Gmail thread the
    #: message itself, independently of the RFC headers.
    thread_id: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    scheduled_at: datetime
    sent_at: datetime | None = None


class AccountSummary(BaseModel):
    email: str
    account_type: str
    status: str
    scopes: list[str]
    created_at: datetime
    next_send_at: datetime | None = None


class QuotaResponse(BaseModel):
    account: str
    messages_sent: int
    recipients_sent: int
    message_soft_limit: int
    recipient_soft_limit: int
    messages_remaining: int
    recipients_remaining: int
    pending_jobs: int
    queue_depth: int
    window_hours: float
    reset_at: datetime | None
    next_send_at: datetime | None


class HistoryItem(BaseModel):
    job_id: int
    account: str
    status: str
    recipients: int
    error_code: str | None
    created_at: datetime
    sent_at: datetime | None
    #: The ids `reply` needs, so an agent can find a reply target from history alone.
    message_id: str | None = None
    thread_id: str | None = None


class ErrorBody(BaseModel):
    code: str
    message: str
    details: dict[str, Any] = {}


class ErrorEnvelope(BaseModel):
    error: ErrorBody


def _decode_attachment(spec: AttachmentIn) -> Attachment:
    """Inline attachments only. File-path attachments are resolved by SendService, which owns
    the allow-list and size checks, so that path is never reachable from a bare schema."""
    if spec.content_base64 is None:
        raise ValidationError.from_exception_data(
            "AttachmentIn",
            [
                {
                    "type": "value_error",
                    "loc": ("content_base64",),
                    "input": spec.content_base64,
                    "ctx": {"error": ValueError("attachment path must be resolved by SendService")},
                }
            ],
        )
    try:
        content = base64.b64decode(spec.content_base64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValidationError.from_exception_data(
            "AttachmentIn",
            [
                {
                    "type": "value_error",
                    "loc": ("content_base64",),
                    "input": spec.content_base64,
                    "ctx": {"error": ValueError(f"attachment content is not valid base64: {exc}")},
                }
            ],
        ) from exc
    return Attachment(filename=spec.filename, content=content, mime_type=spec.mime_type)


def to_outgoing_message(
    req: SendEmailRequest, from_email: str, *, settings: Settings | None = None
) -> OutgoingMessage:
    """Map an API/tool request onto the transport-level message.

    `thread_id` is deliberately not mapped: it is Gmail transport metadata passed to
    `messages.send`, not a MIME header. Threading headers come from `in_reply_to`/`references`.

    Attachments go through `attachments.resolve_attachments`, which is what confines
    path attachments to `GMAIL_AUTOMATOR_ATTACHMENT_ALLOWED_DIRS`. Without `settings` only inline
    content can be resolved, which is the safe default for internal callers.
    """
    specs = [
        (
            spec.filename,
            spec.content_base64,
            spec.path,
            spec.mime_type,
        )
        for spec in req.attachments
    ]
    if settings is not None:
        attachments = resolve_attachments(specs, settings=settings).attachments
    else:
        attachments = tuple(
            _decode_attachment(spec) for spec in req.attachments if spec.content_base64 is not None
        )
    return OutgoingMessage(
        from_email=from_email,
        to=tuple(req.to),
        subject=req.subject,
        body=req.body,
        body_html=req.body_html,
        cc=tuple(req.cc),
        bcc=tuple(req.bcc),
        reply_to=req.reply_to,
        in_reply_to=req.in_reply_to,
        references=req.references,
        attachments=attachments,
    )
