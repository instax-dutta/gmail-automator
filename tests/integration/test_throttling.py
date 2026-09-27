"""Account-level throttling (master plan R3).

Google's `rateLimitExceeded` / `userRateLimitExceeded` are per-user signals, not per-message ones.
When one comes back, retrying each queued job on its own backoff would keep hammering a throttled
account. The worker therefore also pushes the account's pacing cursor, so the rest of the queue
waits too.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from gmail_automator.gmail.client import GoogleApiError
from gmail_automator.worker import Worker

NOW = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)


@pytest.fixture
def worker(wired, sleeper) -> Worker:
    return Worker(wired, worker_id="w", sleeper=sleeper, rand=lambda: 0.0)


def _enqueue(wired, count: int = 1, *, account: str | None = None) -> list[int]:
    resolved = wired.accounts.resolve(account)
    return [
        wired.queue.enqueue(
            account=resolved,
            payload=b"From: me@example.com\r\n\r\nbody",
            recipients=1,
            source="api",
            now=NOW,
        ).id
        for _ in range(count)
    ]


def test_a_rate_limit_pauses_the_account_cursor(worker, wired, connected, fake_transport) -> None:
    fake_transport.script_error(
        GoogleApiError(
            status_code=429, reason="rateLimitExceeded", message="slow down", retry_after=30.0
        )
    )
    _enqueue(wired, count=1)
    assert worker.run_once(now=NOW).action == "retry_scheduled"

    # ordinary pacing alone would have left the cursor 2s out; the rate limit pushes it to 30s
    assert wired.accounts.resolve(None).next_send_at == NOW + timedelta(seconds=30)


def test_a_short_rate_limit_never_shortens_the_pacing_cursor(
    worker, wired, connected, fake_transport
) -> None:
    """A 1s backoff must not pull the cursor back to NOW+1s when pacing already reserved NOW+2s."""
    fake_transport.script_error(
        GoogleApiError(status_code=429, reason="rateLimitExceeded", message="slow down")
    )
    _enqueue(wired, count=1)
    assert worker.run_once(now=NOW).action == "retry_scheduled"
    assert wired.accounts.resolve(None).next_send_at == NOW + timedelta(seconds=2)


def test_the_pause_covers_the_whole_queue(worker, wired, connected, fake_transport) -> None:
    """The second job is already queued behind the cursor, so it waits out the same pause."""
    fake_transport.script_error(
        GoogleApiError(
            status_code=403,
            reason="userRateLimitExceeded",
            message="slow down",
            retry_after=30.0,
        )
    )
    first = _enqueue(wired, count=1)
    assert worker.run_once(now=NOW).action == "retry_scheduled"
    assert wired.accounts.resolve(None).next_send_at == NOW + timedelta(seconds=30)

    # anything enqueued while the account is paused is scheduled behind the pause, not sent
    later = _enqueue(wired, count=2)
    assert all(
        wired.queue.get(job_id).scheduled_at >= NOW + timedelta(seconds=30) for job_id in later
    )
    assert worker.run_once(now=NOW + timedelta(seconds=5)).action == "idle"
    # at the pause boundary the retried job goes first (earliest scheduled_at, then id)
    assert worker.run_once(now=NOW + timedelta(seconds=31)).job_id == first[0]


def test_a_daily_quota_error_does_not_pause_the_account(
    worker, wired, connected, fake_transport
) -> None:
    """A daily limit is not a rate limit: it must not add an artificial delay on top of the
    quota window, which is already the binding constraint."""
    fake_transport.script_error(
        GoogleApiError(status_code=403, reason="dailySendQuotaExceeded", message="limit")
    )
    _enqueue(wired, count=1)
    assert worker.run_once(now=NOW).action == "failed"
    # ordinary pacing only: a daily limit must not add an artificial delay on top of the window
    assert wired.accounts.resolve(None).next_send_at == NOW + timedelta(seconds=2)


def test_a_backend_error_pauses_only_the_job(worker, wired, connected, fake_transport) -> None:
    """A 5xx is about this request, not the account, so the cursor is left alone."""
    fake_transport.script_error(
        GoogleApiError(status_code=503, reason="backendError", message="oops")
    )
    _enqueue(wired, count=1)
    assert worker.run_once(now=NOW).action == "retry_scheduled"
    account = wired.accounts.resolve(None)
    assert account.next_send_at == NOW + timedelta(seconds=2)


def test_another_account_is_not_paused(worker, wired, connected, fake_transport) -> None:
    other = wired.accounts.upsert_oauth_account(
        email="other@example.com", token_uri="http://oauth.test/token", now=NOW
    )
    fake_transport.script_error(
        GoogleApiError(status_code=429, reason="rateLimitExceeded", message="slow down")
    )
    _enqueue(wired, count=1, account="sender@example.com")
    assert worker.run_once(now=NOW).action == "retry_scheduled"
    assert wired.accounts.get("other@example.com").next_send_at is None
    assert other.email == "other@example.com"


def test_the_pause_never_moves_the_cursor_backwards(
    worker, wired, connected, fake_transport
) -> None:
    accounts = wired.accounts
    accounts.advance_pacing("sender@example.com", next_send_at=NOW + timedelta(hours=1))
    fake_transport.script_error(
        GoogleApiError(status_code=429, reason="rateLimitExceeded", message="slow down")
    )
    _enqueue(wired, count=1)
    worker.run_once(now=NOW)
    assert accounts.get("sender@example.com").next_send_at >= NOW + timedelta(hours=1)


def test_exhausted_attempts_do_not_pause_the_account(
    worker, wired, connected, fake_transport
) -> None:
    job_id = _enqueue(wired, count=1)[0]
    clock = wired.clock
    for attempt in range(1, wired.settings.max_attempts + 1):
        fake_transport.script_error(
            GoogleApiError(status_code=429, reason="rateLimitExceeded", message="slow")
        )
        worker.run_once(now=clock.now())
        if attempt < wired.settings.max_attempts:
            clock.advance(timedelta(seconds=64))
    job = wired.queue.get(job_id)
    assert job is not None and job.status == "failed"
    assert job.attempt_count == wired.settings.max_attempts
