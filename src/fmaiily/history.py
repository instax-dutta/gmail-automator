from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from fmaiily.clock import Clock
from fmaiily.models import Account, SendEvent, SendJob
from fmaiily.schemas import HistoryItem, JobStatusResponse

#: Statuses that still count as "in the queue" for history filtering.
ACTIVE = ("pending", "processing")


class HistoryService:
    """Read-only views over past sends, plus retention purges.

    The bodies themselves are not kept (master plan R5): a history row carries the envelope an
    operator or agent needs - who, how many recipients, when, and what happened.
    """

    def __init__(self, *, session_factory: sessionmaker[Session], clock: Clock) -> None:
        self._session_factory = session_factory
        self._clock = clock

    def list_recent(
        self,
        *,
        account_email: str | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> list[HistoryItem]:
        limit = max(1, min(limit, 500))
        with self._session_factory() as session:
            query = (
                select(SendJob, Account.email)
                .join(SendJob.account)
                .order_by(SendJob.id.desc())
                .limit(limit)
            )
            if account_email:
                query = query.where(Account.email == account_email.strip().lower())
            if status:
                query = query.where(SendJob.status == status)
            rows = session.execute(query).all()
        return [
            HistoryItem(
                job_id=job.id,
                account=email,
                status=job.status,
                recipients=job.recipients,
                error_code=job.error_code,
                created_at=job.created_at,
                sent_at=job.sent_at,
            )
            for job, email in rows
        ]

    def job_status(self, job_id: int) -> JobStatusResponse | None:
        with self._session_factory() as session:
            row = session.execute(
                select(SendJob, Account.email).join(SendJob.account).where(SendJob.id == job_id)
            ).first()
            if row is None:
                return None
            job, email = row
            return JobStatusResponse(
                job_id=job.id,
                status=job.status,
                account=email,
                recipients=job.recipients,
                attempts=job.attempt_count,
                message_id=job.gmail_message_id,
                error_code=job.error_code,
                error_message=job.error_message,
                scheduled_at=job.scheduled_at,
                sent_at=job.sent_at,
            )

    def events(self, job_id: int) -> list[SendEvent]:
        with self._session_factory() as session:
            rows = list(
                session.scalars(
                    select(SendEvent).where(SendEvent.job_id == job_id).order_by(SendEvent.id)
                )
            )
            for row in rows:
                session.expunge(row)
            return rows

    def purge_older_than(self, *, days: int, now: datetime | None = None) -> int:
        """Drop terminal history rows past the retention window. Returns rows removed."""
        now = now or self._clock.now()
        cutoff = now - timedelta(days=days)
        with self._session_factory() as session:
            stale = list(
                session.scalars(
                    select(SendJob).where(
                        SendJob.status.notin_(ACTIVE),
                        SendJob.created_at < cutoff,
                    )
                )
            )
            job_ids = [job.id for job in stale]
            if job_ids:
                for event in session.scalars(
                    select(SendEvent).where(SendEvent.job_id.in_(job_ids))
                ):
                    session.delete(event)
                for job in stale:
                    session.delete(job)
            session.commit()
            return len(stale)


__all__ = ["ACTIVE", "HistoryService"]
