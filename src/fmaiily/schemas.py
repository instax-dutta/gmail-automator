from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, EmailStr, Field, model_validator

SCHEMA_VERSION = "v1"


class AttachmentIn(BaseModel):
    filename: str = Field(min_length=1, max_length=255)
    content_base64: str | None = None
    path: str | None = None
    mime_type: str = "application/octet-stream"

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
    message_id: str | None = None
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


class ErrorBody(BaseModel):
    code: str
    message: str
    details: dict[str, Any] = {}


class ErrorEnvelope(BaseModel):
    error: ErrorBody
