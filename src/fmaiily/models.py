from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from fmaiily.db import Base, UTCDateTime


def utcnow() -> datetime:
    return datetime.now(UTC)


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    auth_type: Mapped[str] = mapped_column(String(20), default="oauth")  # oauth | service_account
    account_type: Mapped[str] = mapped_column(String(20), default="personal")
    access_token_enc: Mapped[str | None] = mapped_column(Text, default=None)
    refresh_token_enc: Mapped[str | None] = mapped_column(Text, default=None)
    token_expiry: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)
    scopes: Mapped[list[str]] = mapped_column(JSON, default=list)
    token_uri: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(20), default="active")  # active|revoked|error
    last_refresh_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)
    last_refresh_error: Mapped[str | None] = mapped_column(Text, default=None)

    # Column defaults are the DB-level fallback; AccountService sets them explicitly from
    # Settings at connect time so an operator's configuration always wins.
    daily_message_limit: Mapped[int] = mapped_column(Integer, default=500)
    daily_recipient_limit: Mapped[int] = mapped_column(Integer, default=500)
    soft_limit_ratio: Mapped[float] = mapped_column(Float, default=0.85)
    send_interval_seconds: Mapped[float] = mapped_column(Float, default=2.0)
    next_send_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None, index=True)

    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)

    jobs: Mapped[list[SendJob]] = relationship(back_populates="account")


class SendJob(Base):
    __tablename__ = "send_jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    raw_payload_enc: Mapped[str | None] = mapped_column(Text, default=None)
    payload_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)
    recipients: Mapped[int] = mapped_column(Integer)
    thread_id: Mapped[str | None] = mapped_column(String(64), default=None)
    source: Mapped[str] = mapped_column(String(20))  # api | mcp | cli | internal
    api_key_id: Mapped[int | None] = mapped_column(ForeignKey("api_keys.id"), default=None)
    idempotency_scope: Mapped[str | None] = mapped_column(String(80), default=None)
    idempotency_key: Mapped[str | None] = mapped_column(String(120), default=None)
    request_hash: Mapped[str | None] = mapped_column(String(64), default=None)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=5)
    scheduled_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None, index=True)
    worker_id: Mapped[str | None] = mapped_column(String(80), default=None)
    gmail_message_id: Mapped[str | None] = mapped_column(String(64), default=None)
    gmail_thread_id: Mapped[str | None] = mapped_column(String(64), default=None)
    error_code: Mapped[str | None] = mapped_column(String(50), default=None)
    error_message: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)
    sent_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)

    account: Mapped[Account] = relationship(back_populates="jobs")

    __table_args__ = (
        UniqueConstraint("idempotency_scope", "idempotency_key", name="uq_jobs_scope_idem"),
        Index("ix_jobs_account_status", "account_id", "status"),
        Index("ix_jobs_account_sent", "account_id", "sent_at"),
    )


class SendEvent(Base):
    __tablename__ = "send_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("send_jobs.id"), index=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True)
    # enqueued|claimed|succeeded|failed|retry_scheduled|rejected|requeued
    event: Mapped[str] = mapped_column(String(30))
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    error_code: Mapped[str | None] = mapped_column(String(50), default=None)
    error_message: Mapped[str | None] = mapped_column(Text, default=None)
    latency_ms: Mapped[int | None] = mapped_column(Integer, default=None)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)


class ApiKeyRow(Base):
    __tablename__ = "api_keys"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80))
    key_prefix: Mapped[str] = mapped_column(String(16), unique=True, index=True)
    key_hash: Mapped[str] = mapped_column(String(64))
    scopes: Mapped[list[str]] = mapped_column(JSON, default=list)
    allowed_accounts: Mapped[list[str] | None] = mapped_column(JSON, default=None)
    rate_limit_per_minute: Mapped[int | None] = mapped_column(Integer, default=None)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)


class OAuthState(Base):
    __tablename__ = "oauth_states"

    state: Mapped[str] = mapped_column(String(64), primary_key=True)
    redirect_uri: Mapped[str] = mapped_column(String(500))
    account_hint: Mapped[str | None] = mapped_column(String(320), default=None)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    consumed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)
