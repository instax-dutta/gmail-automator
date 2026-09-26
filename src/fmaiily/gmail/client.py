"""Gmail transport: the only module in the codebase allowed to touch ``googleapiclient``.

Everything above this seam sees a `GmailTransport` and a small, fully-typed result object, which
is what makes the rest of the gateway testable without a network. Error classification lives here
too: the worker and the retry policy must not have to understand Google's error envelope.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

import httplib2
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

#: Reasons that will not improve by retrying. Per Google's own documentation a daily-quota
#: rejection can persist for hours, so hot-retrying it only burns quota and delays recovery (R3).
TERMINAL_REASONS: frozenset[str] = frozenset(
    {
        "dailySendQuotaExceeded",
        "sendDenied",
        "mailNotSent",
        "insufficientPermissions",
        "forbidden",
        "invalid",
        "invalidArgument",
        "backendErrorInvalidToken",
    }
)

#: Reasons that are explicitly transient.
RETRYABLE_REASONS: frozenset[str] = frozenset(
    {
        "rateLimitExceeded",
        "userRateLimitExceeded",
        "backendError",
        "internalFailure",
        "serviceUnavailable",
        "quotaExceeded",
    }
)


@dataclass(frozen=True)
class SendResult:
    message_id: str
    thread_id: str | None
    label_ids: tuple[str, ...]


class GoogleApiError(Exception):
    """A normalized Gmail API failure."""

    def __init__(
        self,
        *,
        status_code: int | None,
        reason: str | None,
        message: str,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.reason = reason
        self.message = message
        self.retry_after = retry_after

    @property
    def retryable(self) -> bool:
        if self.reason in TERMINAL_REASONS:
            return False
        if self.reason in RETRYABLE_REASONS:
            return True
        if self.status_code == 429:
            return True
        return self.status_code is not None and self.status_code >= 500

    def __repr__(self) -> str:
        return (
            f"GoogleApiError(status_code={self.status_code!r}, reason={self.reason!r}, "
            f"retryable={self.retryable!r})"
        )


class AuthExpired(GoogleApiError):
    """Gmail rejected the access token; refresh once and retry."""


class GmailTransport(Protocol):
    def send_raw(
        self, *, email: str, access_token: str, raw_b64url: str, thread_id: str | None = None
    ) -> SendResult: ...


class GoogleGmailTransport:
    """`GmailTransport` backed by google-api-python-client."""

    def __init__(
        self,
        *,
        api_endpoint: str,
        timeout: float = 30.0,
        http: Any | None = None,
    ) -> None:
        self.api_endpoint = api_endpoint
        self.timeout = timeout
        self._http = http

    def _service(self, access_token: str) -> Any:
        http = self._http or httplib2.Http(timeout=self.timeout)
        return build(
            "gmail",
            "v1",
            http=http,
            client_options={"api_endpoint": self.api_endpoint},
            cache_discovery=False,
        )

    def send_raw(
        self, *, email: str, access_token: str, raw_b64url: str, thread_id: str | None = None
    ) -> SendResult:
        service = self._service(access_token)
        body: dict[str, Any] = {"raw": raw_b64url}
        if thread_id:
            body["threadId"] = thread_id
        request = service.users().messages().send(userId=email, body=body)
        # The bearer token is attached here rather than through AuthorizedHttp so the transport
        # keeps no credential object with a refresh path of its own: the worker owns refreshing.
        request.headers["authorization"] = f"Bearer {access_token}"
        try:
            payload = request.execute()
        except HttpError as exc:
            raise _translate(exc) from exc
        return SendResult(
            message_id=str(payload.get("id", "")),
            thread_id=payload.get("threadId"),
            label_ids=tuple(payload.get("labelIds") or ()),
        )


def _translate(exc: HttpError) -> GoogleApiError:
    status = exc.resp.status if exc.resp is not None else None
    reason, message = _parse_error_body(exc.content)
    retry_after = _parse_retry_after(exc.resp)
    error_type = AuthExpired if status == 401 else GoogleApiError
    return error_type(status_code=status, reason=reason, message=message, retry_after=retry_after)


def _parse_error_body(content: str | bytes | None) -> tuple[str | None, str]:
    import json

    if not content:
        return None, "Gmail API request failed"
    text = content.decode("utf-8", "replace") if isinstance(content, bytes) else content
    try:
        body = json.loads(text)
    except ValueError:
        return None, text[:500]
    if not isinstance(body, dict):
        return None, text[:500]
    error = body.get("error")
    if not isinstance(error, dict):
        return None, str(error or body)[:500]
    reason: str | None = None
    for entry in error.get("errors") or []:
        if isinstance(entry, dict) and entry.get("reason"):
            reason = str(entry["reason"])
            break
    message = str(error.get("message") or error.get("status") or "Gmail API request failed")
    return reason, message[:500]


def _parse_retry_after(resp: Any) -> float | None:
    if resp is None:
        return None
    headers = getattr(resp, "headers", None) or {}
    raw = headers.get("retry-after") if hasattr(headers, "get") else None
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None
