from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from gmail_automator.clock import Clock
from gmail_automator.errors import QuotaExceeded
from gmail_automator.models import Account, SendJob

#: Gmail measures its daily caps over a rolling 24 hour window, not a calendar day.
WINDOW = timedelta(hours=24)

#: Statuses that hold a reservation against the window: the job is going to consume capacity
#: unless it fails, and failing does not hand the reservation back (the message may have been
#: partially delivered), so both are counted.
RESERVING_STATUSES = ("pending", "processing")
COMPLETED_STATUS = "sent"


@dataclass(frozen=True)
class QuotaSnapshot:
    account_email: str
    messages_sent: int
    recipients_sent: int
    message_limit: int
    recipient_limit: int
    message_soft_limit: int
    recipient_soft_limit: int
    messages_remaining: int
    recipients_remaining: int
    pending_jobs: int
    pending_recipients: int
    queue_depth: int
    next_send_at: datetime | None
    window_hours: float
    reset_at: datetime | None


class QuotaService:
    """Rolling 24 hour quota accounting, derived from `send_jobs` (master plan R6).

    There is no counter table: `used` comes from completed sends, `reserved` from in-flight jobs.
    That keeps the numbers inspectable with a single SQL query and makes the whole policy testable
    with an injected clock.
    """

    def __init__(self, *, session_factory: sessionmaker[Session], clock: Clock) -> None:
        self._session_factory = session_factory
        self._clock = clock

    def snapshot(self, account: Account, *, now: datetime | None = None) -> QuotaSnapshot:
        now = now or self._clock.now()
        window_start = now - WINDOW
        with self._session_factory() as session:
            sent_messages, sent_recipients, oldest_sent = self._completed(
                session, account.id, window_start
            )
            pending_jobs, pending_recipients = self._reserved(session, account.id)

        message_soft, recipient_soft = soft_limits(account)
        return QuotaSnapshot(
            account_email=account.email,
            messages_sent=sent_messages,
            recipients_sent=sent_recipients,
            message_limit=account.daily_message_limit,
            recipient_limit=account.daily_recipient_limit,
            message_soft_limit=message_soft,
            recipient_soft_limit=recipient_soft,
            messages_remaining=max(0, message_soft - (sent_messages + pending_jobs)),
            recipients_remaining=max(0, recipient_soft - (sent_recipients + pending_recipients)),
            pending_jobs=pending_jobs,
            pending_recipients=pending_recipients,
            queue_depth=pending_jobs,
            next_send_at=account.next_send_at,
            window_hours=WINDOW.total_seconds() / 3600,
            reset_at=(oldest_sent + WINDOW) if oldest_sent is not None else None,
        )

    def check(self, account: Account, recipients: int, *, now: datetime | None = None) -> None:
        """Raise `QuotaExceeded` when a send would cross a soft limit.

        R3: the gateway must refuse *before* calling Gmail. The message cap is evaluated first so
        the error a caller sees names the binding constraint.
        """
        now = now or self._clock.now()
        snap = self.snapshot(account, now=now)

        if snap.messages_sent + snap.pending_jobs + 1 > snap.message_soft_limit:
            raise QuotaExceeded(
                f"daily message soft limit reached for {account.email}; "
                f"{snap.messages_sent + snap.pending_jobs} of {snap.message_soft_limit} used",
                details={
                    "account": account.email,
                    "resource": "messages",
                    "used": snap.messages_sent + snap.pending_jobs,
                    "soft_limit": snap.message_soft_limit,
                    "hard_limit": snap.message_limit,
                    "reset_at": _iso(snap.reset_at),
                },
            )

        if recipients > 0 and (
            snap.recipients_sent + snap.pending_recipients + recipients > snap.recipient_soft_limit
        ):
            raise QuotaExceeded(
                f"daily recipient soft limit reached for {account.email}; "
                f"{snap.recipients_sent + snap.pending_recipients} of "
                f"{snap.recipient_soft_limit} used, {recipients} requested",
                details={
                    "account": account.email,
                    "resource": "recipients",
                    "used": snap.recipients_sent + snap.pending_recipients,
                    "requested": recipients,
                    "soft_limit": snap.recipient_soft_limit,
                    "hard_limit": snap.recipient_limit,
                    "reset_at": _iso(snap.reset_at),
                },
            )

    def _completed(
        self, session: Session, account_id: int, window_start: datetime
    ) -> tuple[int, int, datetime | None]:
        row = session.execute(
            select(
                func.count(SendJob.id),
                func.coalesce(func.sum(SendJob.recipients), 0),
                func.min(SendJob.sent_at),
            ).where(
                SendJob.account_id == account_id,
                SendJob.status == COMPLETED_STATUS,
                SendJob.sent_at > window_start,
            )
        ).one()
        return int(row[0]), int(row[1]), row[2]

    def _reserved(self, session: Session, account_id: int) -> tuple[int, int]:
        row = session.execute(
            select(
                func.count(SendJob.id),
                func.coalesce(func.sum(SendJob.recipients), 0),
            ).where(
                SendJob.account_id == account_id,
                SendJob.status.in_(RESERVING_STATUSES),
            )
        ).one()
        return int(row[0]), int(row[1])


def soft_limits(account: Account) -> tuple[int, int]:
    """Hard limit scaled by the account's ratio, floored (master plan R3)."""
    return (
        math.floor(account.daily_message_limit * account.soft_limit_ratio),
        math.floor(account.daily_recipient_limit * account.soft_limit_ratio),
    )


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


__all__ = [
    "RESERVING_STATUSES",
    "WINDOW",
    "QuotaExceeded",
    "QuotaService",
    "QuotaSnapshot",
    "soft_limits",
]
