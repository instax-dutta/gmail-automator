from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from fmaiily.errors import GatewayError
from fmaiily.gmail.client import AuthExpired, GoogleApiError

Rand = Callable[[], float]

#: R3: a daily-quota rejection can persist for hours, so it is terminal for the job. Retrying it
#: only burns the remaining daily budget and delays the operator's recovery.
DAILY_QUOTA_REASONS: frozenset[str] = frozenset(
    {"dailySendQuotaExceeded", "mailNotSent", "sendDenied"}
)
DAILY_QUOTA_CODES: frozenset[str] = frozenset({"daily_send_quota_exceeded"})

#: Transient transport conditions worth another attempt.
TRANSIENT_EXCEPTIONS: tuple[type[BaseException], ...] = (
    TimeoutError,
    ConnectionError,
    OSError,
)


@dataclass(frozen=True)
class RetryDecision:
    action: Literal["retry", "fail", "refresh"]
    error_code: str
    message: str
    delay_seconds: float
    attempt: int
    reason: str | None = None


def classify(exc: BaseException) -> str:
    """Map an exception onto a stable, agent-facing error code."""
    if isinstance(exc, GoogleApiError):
        if exc.reason in DAILY_QUOTA_REASONS:
            return "daily_send_quota_exceeded"
        if exc.status_code == 429 or exc.reason in {
            "rateLimitExceeded",
            "userRateLimitExceeded",
        }:
            return "gmail_rate_limited"
        if exc.status_code == 401:
            return "auth_expired"
        if exc.status_code == 403:
            return "gmail_forbidden"
        if exc.status_code == 404:
            return "gmail_not_found"
        if exc.status_code is not None and exc.status_code >= 500:
            return "gmail_backend_error"
        return "gmail_invalid_request"
    if isinstance(exc, GatewayError):
        return exc.code
    if isinstance(exc, TRANSIENT_EXCEPTIONS):
        return "transport_error"
    return "internal_error"


def compute_delay(
    *,
    attempt: int,
    base: float,
    cap: float,
    retry_after: float | None,
    rand: Rand,
) -> float:
    """`min(2^(attempt-1) + jitter, cap)`, never shorter than a server-provided `Retry-After`."""
    exponential = base * (2 ** max(attempt - 1, 0)) + rand()
    delay = min(float(exponential), cap)
    if retry_after is not None and retry_after > delay:
        return float(retry_after)
    return delay


def decide(
    exc: BaseException,
    *,
    attempt: int,
    max_attempts: int,
    base: float,
    cap: float,
    rand: Rand,
) -> RetryDecision:
    """Decide what the worker does with a failed send.

    `refresh` means the access token was rejected: the caller refreshes and retries immediately
    instead of waiting out a backoff, because a stale token is not a rate-limit problem.
    """
    code = classify(exc)
    message = str(getattr(exc, "message", None) or exc)
    reason = getattr(exc, "reason", None)
    retry_after = getattr(exc, "retry_after", None)

    if isinstance(exc, AuthExpired):
        return RetryDecision(
            action="refresh",
            error_code=code,
            message=message,
            delay_seconds=0.0,
            attempt=attempt,
            reason=reason,
        )

    if code in DAILY_QUOTA_CODES:
        return RetryDecision(
            action="fail",
            error_code=code,
            message=message,
            delay_seconds=0.0,
            attempt=attempt,
            reason=reason,
        )

    retryable = isinstance(exc, GoogleApiError) and exc.retryable
    retryable = retryable or (isinstance(exc, TRANSIENT_EXCEPTIONS))

    if not retryable or attempt >= max_attempts:
        return RetryDecision(
            action="fail",
            error_code=code,
            message=message,
            delay_seconds=0.0,
            attempt=attempt,
            reason=reason,
        )

    return RetryDecision(
        action="retry",
        error_code=code,
        message=message,
        delay_seconds=compute_delay(
            attempt=attempt, base=base, cap=cap, retry_after=retry_after, rand=rand
        ),
        attempt=attempt,
        reason=reason,
    )


__all__ = [
    "DAILY_QUOTA_CODES",
    "DAILY_QUOTA_REASONS",
    "Rand",
    "RetryDecision",
    "classify",
    "compute_delay",
    "decide",
]
