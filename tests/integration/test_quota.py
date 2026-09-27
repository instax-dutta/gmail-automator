import math
from datetime import UTC, datetime, timedelta

import pytest

from gmail_automator.accounts import AccountService
from gmail_automator.errors import QuotaExceeded
from gmail_automator.models import Account, SendJob
from gmail_automator.quota import QuotaService, QuotaSnapshot

NOW = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
WINDOW = timedelta(hours=24)


@pytest.fixture
def account(seeded_engine, session_factory, fake_clock, settings) -> Account:
    return AccountService(
        session_factory=session_factory, clock=fake_clock, settings=settings
    ).upsert_oauth_account(
        email="me@example.com",
        scopes=["https://www.googleapis.com/auth/gmail.send"],
        token_uri="https://oauth2.googleapis.com/token",
        now=NOW,
    )


@pytest.fixture
def quota(session_factory, fake_clock) -> QuotaService:
    return QuotaService(session_factory=session_factory, clock=fake_clock)


@pytest.fixture
def accounts(session_factory, seeded_engine, fake_clock, settings) -> AccountService:
    return AccountService(session_factory=session_factory, clock=fake_clock, settings=settings)


def _job(
    session_factory,
    account: Account,
    *,
    status: str,
    recipients: int = 1,
    sent_at: datetime | None = None,
    scheduled_at: datetime = NOW,
    created_at: datetime = NOW,
) -> SendJob:
    with session_factory() as session:
        job = SendJob(
            account_id=account.id,
            status=status,
            recipients=recipients,
            source="api",
            scheduled_at=scheduled_at,
            sent_at=sent_at,
            created_at=created_at,
            updated_at=created_at,
        )
        session.add(job)
        session.commit()
        job_id = job.id
    assert job_id is not None
    return _reload(session_factory, job_id)


def _reload(session_factory, job_id: int) -> SendJob:
    with session_factory() as session:
        job = session.get(SendJob, job_id)
        session.expunge(job)
        return job


def test_empty_account_has_full_capacity(quota: QuotaService, account: Account) -> None:
    snap = quota.snapshot(account, now=NOW)
    assert isinstance(snap, QuotaSnapshot)
    assert snap.account_email == "me@example.com"
    assert snap.messages_sent == 0
    assert snap.recipients_sent == 0
    assert snap.message_soft_limit == math.floor(500 * 0.85)  # 425
    assert snap.recipient_soft_limit == 425
    assert snap.messages_remaining == 425
    assert snap.recipients_remaining == 425
    assert snap.queue_depth == 0
    assert snap.reset_at is None
    assert snap.window_hours == 24.0


def test_soft_limits_scale_with_account_ratio(
    quota: QuotaService, account: Account, session_factory
):
    with session_factory() as session:
        row = session.get(Account, account.id)
        row.daily_message_limit = 2000  # Workspace
        row.daily_recipient_limit = 10000
        row.soft_limit_ratio = 0.9
        session.commit()
    with session_factory() as session:
        reloaded = session.get(Account, account.id)
        session.expunge(reloaded)
    snap = quota.snapshot(reloaded, now=NOW)
    assert snap.message_soft_limit == 1800
    assert snap.recipient_soft_limit == 9000


def test_completed_sends_inside_the_window_are_counted(
    quota: QuotaService, account: Account, session_factory
) -> None:
    _job(session_factory, account, status="sent", recipients=3, sent_at=NOW - timedelta(hours=1))
    _job(session_factory, account, status="sent", recipients=2, sent_at=NOW - timedelta(hours=23))
    snap = quota.snapshot(account, now=NOW)
    assert snap.messages_sent == 2
    assert snap.recipients_sent == 5
    assert snap.messages_remaining == 425 - 2


def test_sends_older_than_the_window_have_expired(
    quota: QuotaService, account: Account, session_factory
) -> None:
    _job(session_factory, account, status="sent", recipients=3, sent_at=NOW - WINDOW)
    _job(session_factory, account, status="sent", recipients=3, sent_at=NOW - timedelta(hours=25))
    snap = quota.snapshot(account, now=NOW)
    assert snap.messages_sent == 0
    assert snap.recipients_sent == 0
    assert snap.messages_remaining == 425


def test_pending_and_processing_jobs_are_reserved(
    quota: QuotaService, account: Account, session_factory
) -> None:
    _job(session_factory, account, status="pending", recipients=2)
    _job(session_factory, account, status="processing", recipients=4)
    snap = quota.snapshot(account, now=NOW)
    assert snap.pending_jobs == 2
    assert snap.pending_recipients == 6
    assert snap.queue_depth == 2
    assert snap.messages_remaining == 425 - 2
    assert snap.recipients_remaining == 425 - 6


def test_failed_jobs_do_not_consume_capacity(
    quota: QuotaService, account: Account, session_factory
) -> None:
    _job(session_factory, account, status="failed", recipients=2, sent_at=None)
    _job(session_factory, account, status="rejected", recipients=2)
    snap = quota.snapshot(account, now=NOW)
    assert snap.messages_sent == 0
    assert snap.pending_jobs == 0
    assert snap.messages_remaining == 425


def test_reset_at_points_at_the_oldest_send_leaving_the_window(
    quota: QuotaService, account: Account, session_factory
) -> None:
    _job(session_factory, account, status="sent", sent_at=NOW - timedelta(hours=20))
    _job(session_factory, account, status="sent", sent_at=NOW - timedelta(hours=2))
    snap = quota.snapshot(account, now=NOW)
    assert snap.reset_at == NOW + timedelta(hours=4)


def test_next_send_at_reflects_the_pacing_cursor(
    quota: QuotaService, account: Account, accounts: AccountService
) -> None:
    accounts.advance_pacing("me@example.com", next_send_at=NOW + timedelta(seconds=2))
    snap = quota.snapshot(accounts.get("me@example.com"), now=NOW)
    assert snap.next_send_at == NOW + timedelta(seconds=2)


def test_check_allows_a_send_within_the_limit(quota: QuotaService, account: Account) -> None:
    quota.check(account, recipients=1, now=NOW)  # must not raise


def test_check_blocks_the_message_that_would_cross_the_soft_limit(
    quota: QuotaService, account: Account, session_factory
) -> None:
    for _ in range(425):
        _job(session_factory, account, status="sent", sent_at=NOW - timedelta(minutes=1))
    with pytest.raises(QuotaExceeded) as excinfo:
        quota.check(account, recipients=1, now=NOW)
    assert excinfo.value.code == "quota_exceeded"
    assert excinfo.value.details["resource"] == "messages"
    assert excinfo.value.details["account"] == "me@example.com"
    assert excinfo.value.details["soft_limit"] == 425


def test_check_allows_the_exact_boundary_send(
    quota: QuotaService, account: Account, session_factory
) -> None:
    for _ in range(424):
        _job(session_factory, account, status="sent", sent_at=NOW - timedelta(minutes=1))
    quota.check(account, recipients=1, now=NOW)


def test_check_blocks_on_recipients_independently(
    quota: QuotaService, account: Account, session_factory
) -> None:
    # 100 of 425 messages used, but 400 of 425 recipients: the recipient cap binds first
    for _ in range(100):
        _job(session_factory, account, status="sent", recipients=4, sent_at=NOW)
    quota.check(account, recipients=25, now=NOW)  # 425 exactly: still allowed
    with pytest.raises(QuotaExceeded) as excinfo:
        quota.check(account, recipients=26, now=NOW)
    assert excinfo.value.details["resource"] == "recipients"


def test_check_counts_reserved_jobs_against_the_limit(
    quota: QuotaService, account: Account, session_factory
) -> None:
    for _ in range(425):
        _job(session_factory, account, status="pending", recipients=1)
    with pytest.raises(QuotaExceeded):
        quota.check(account, recipients=1, now=NOW)


def test_check_rejects_a_single_message_larger_than_the_recipient_cap(
    quota: QuotaService, account: Account
) -> None:
    with pytest.raises(QuotaExceeded) as excinfo:
        quota.check(account, recipients=5000, now=NOW)
    assert excinfo.value.details["resource"] == "recipients"


def test_check_message_cap_is_evaluated_before_recipients(
    quota: QuotaService, account: Account, session_factory
) -> None:
    for _ in range(425):
        _job(session_factory, account, status="sent", recipients=500, sent_at=NOW)
    with pytest.raises(QuotaExceeded) as excinfo:
        quota.check(account, recipients=500, now=NOW)
    assert excinfo.value.details["resource"] == "messages"


def test_remaining_counts_never_go_negative(
    quota: QuotaService, account: Account, session_factory
) -> None:
    _job(session_factory, account, status="pending", recipients=10_000)
    snap = quota.snapshot(account, now=NOW)
    assert snap.recipients_remaining == 0
    assert snap.messages_remaining == 424


def test_quota_is_scoped_per_account(
    quota: QuotaService, account: Account, accounts: AccountService, session_factory
) -> None:
    other = accounts.upsert_oauth_account(
        email="other@example.com", token_uri="https://oauth2.googleapis.com/token", now=NOW
    )
    for _ in range(425):
        _job(session_factory, account, status="sent", sent_at=NOW)
    snap = quota.snapshot(other, now=NOW)
    assert snap.messages_sent == 0
    assert snap.messages_remaining == 425
