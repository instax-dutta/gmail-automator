import random

import pytest

from gmail_automator.errors import SendFailed
from gmail_automator.gmail.client import AuthExpired, GoogleApiError
from gmail_automator.retry import RetryDecision, classify, compute_delay, decide

BASE = 1.0
CAP = 64.0
MAX_ATTEMPTS = 5


def _err(status: int | None, reason: str | None, retry_after: float | None = None):
    return GoogleApiError(status_code=status, reason=reason, message="x", retry_after=retry_after)


def test_classify_maps_transport_failures_to_stable_codes() -> None:
    assert classify(_err(429, "rateLimitExceeded")) == "gmail_rate_limited"
    assert classify(_err(403, "userRateLimitExceeded")) == "gmail_rate_limited"
    assert classify(_err(500, "backendError")) == "gmail_backend_error"
    assert classify(_err(400, "invalid")) == "gmail_invalid_request"
    assert classify(_err(403, "insufficientPermissions")) == "gmail_forbidden"
    assert classify(_err(404, "notFound")) == "gmail_not_found"


def test_daily_quota_is_its_own_code_and_never_retried() -> None:
    decision = decide(
        _err(403, "dailySendQuotaExceeded"),
        attempt=1,
        max_attempts=MAX_ATTEMPTS,
        base=BASE,
        cap=CAP,
        rand=lambda: 0.0,
    )
    assert decision.action == "fail"
    assert decision.error_code == "daily_send_quota_exceeded"
    assert decision.reason == "dailySendQuotaExceeded"


def test_rate_limit_is_retried_with_backoff() -> None:
    decision = decide(
        _err(429, "rateLimitExceeded"),
        attempt=1,
        max_attempts=MAX_ATTEMPTS,
        base=BASE,
        cap=CAP,
        rand=lambda: 0.0,
    )
    assert decision.action == "retry"
    assert decision.delay_seconds == pytest.approx(1.0)
    assert decision.error_code == "gmail_rate_limited"


def test_backoff_doubles_per_attempt() -> None:
    # attempts 1..4 are retried; attempt 5 is the last, so it fails instead of scheduling again
    delays = [
        decide(
            _err(503, "backendError"),
            attempt=attempt,
            max_attempts=MAX_ATTEMPTS,
            base=BASE,
            cap=CAP,
            rand=lambda: 0.0,
        ).delay_seconds
        for attempt in range(1, MAX_ATTEMPTS)
    ]
    assert delays == [1.0, 2.0, 4.0, 8.0]


def test_backoff_is_capped() -> None:
    delay = compute_delay(attempt=20, base=BASE, cap=CAP, retry_after=None, rand=lambda: 0.5)
    assert delay == CAP


def test_backoff_includes_jitter() -> None:
    low = compute_delay(attempt=3, base=BASE, cap=CAP, retry_after=None, rand=lambda: 0.0)
    high = compute_delay(attempt=3, base=BASE, cap=CAP, retry_after=None, rand=lambda: 0.9)
    assert low == pytest.approx(4.0)
    assert high == pytest.approx(4.9)


def test_retry_after_header_wins_when_larger() -> None:
    delay = compute_delay(attempt=1, base=BASE, cap=CAP, retry_after=30.0, rand=lambda: 0.0)
    assert delay == 30.0


def test_retry_after_header_does_not_shorten_the_backoff() -> None:
    delay = compute_delay(attempt=4, base=BASE, cap=CAP, retry_after=1.0, rand=lambda: 0.0)
    assert delay == pytest.approx(8.0)


def test_retry_after_beyond_the_cap_is_honored() -> None:
    # Google can ask for a long wait; the server is authoritative about its own limits
    delay = compute_delay(attempt=1, base=BASE, cap=CAP, retry_after=120.0, rand=lambda: 0.0)
    assert delay == 120.0


def test_attempts_are_exhausted_after_max_attempts() -> None:
    decision = decide(
        _err(429, "rateLimitExceeded"),
        attempt=MAX_ATTEMPTS,
        max_attempts=MAX_ATTEMPTS,
        base=BASE,
        cap=CAP,
        rand=lambda: 0.0,
    )
    assert decision.action == "fail"
    assert decision.error_code == "gmail_rate_limited"
    assert decision.attempt == MAX_ATTEMPTS


def test_non_retryable_fails_on_the_first_attempt() -> None:
    decision = decide(
        _err(400, "invalid"),
        attempt=1,
        max_attempts=MAX_ATTEMPTS,
        base=BASE,
        cap=CAP,
        rand=lambda: 0.0,
    )
    assert decision.action == "fail"


def test_auth_expired_asks_for_a_refresh_rather_than_a_backoff() -> None:
    decision = decide(
        AuthExpired(status_code=401, reason="authError", message="Invalid Credentials"),
        attempt=1,
        max_attempts=MAX_ATTEMPTS,
        base=BASE,
        cap=CAP,
        rand=lambda: 0.0,
    )
    assert isinstance(decision, RetryDecision)
    assert decision.action == "refresh"
    assert decision.error_code == "auth_expired"


def test_domain_errors_fail_without_retry() -> None:
    decision = decide(
        SendFailed("token refresh failed"),
        attempt=1,
        max_attempts=MAX_ATTEMPTS,
        base=BASE,
        cap=CAP,
        rand=lambda: 0.0,
    )
    assert decision.action == "fail"
    assert decision.error_code == "send_failed"


def test_unexpected_exceptions_are_retried_as_transient() -> None:
    decision = decide(
        TimeoutError("socket timed out"),
        attempt=1,
        max_attempts=MAX_ATTEMPTS,
        base=BASE,
        cap=CAP,
        rand=lambda: 0.0,
    )
    assert decision.action == "retry"
    assert decision.error_code == "transport_error"


def test_unexpected_exception_exhaustion_fails() -> None:
    decision = decide(
        TimeoutError("socket timed out"),
        attempt=MAX_ATTEMPTS,
        max_attempts=MAX_ATTEMPTS,
        base=BASE,
        cap=CAP,
        rand=lambda: 0.0,
    )
    assert decision.action == "fail"


def test_real_random_source_produces_varied_delays() -> None:
    rng = random.Random(1234)
    delays = {
        compute_delay(attempt=3, base=BASE, cap=CAP, retry_after=None, rand=rng.random)
        for _ in range(20)
    }
    assert len(delays) > 1
    assert all(4.0 <= d <= 5.0 for d in delays)


def test_decision_carries_the_attempt_number() -> None:
    decision = decide(
        _err(429, "rateLimitExceeded"),
        attempt=2,
        max_attempts=MAX_ATTEMPTS,
        base=BASE,
        cap=CAP,
        rand=lambda: 0.0,
    )
    assert decision.attempt == 2


def test_message_is_preserved_for_the_send_history() -> None:
    decision = decide(
        _err(400, "invalid", retry_after=None),
        attempt=1,
        max_attempts=MAX_ATTEMPTS,
        base=BASE,
        cap=CAP,
        rand=lambda: 0.0,
    )
    assert decision.message == "x"
