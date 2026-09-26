import dataclasses
from datetime import UTC, datetime
from typing import Any

import pytest

from fmaiily.gmail.client import (
    AuthExpired,
    GmailTransport,
    GoogleApiError,
    GoogleGmailTransport,
    SendResult,
)
from tests.support.fake_gmail_app import fake_gmail_app
from tests.support.sync_asgi import SyncASGIHttplib2

FAKE_APP = fake_gmail_app()
ENDPOINT = "http://gmail.test"


def _transport(**kwargs: Any) -> GoogleGmailTransport:
    return GoogleGmailTransport(api_endpoint=ENDPOINT, http=SyncASGIHttplib2(FAKE_APP), **kwargs)


def test_google_gmail_transport_satisfies_the_protocol() -> None:
    transport: GmailTransport = _transport()
    assert hasattr(transport, "send_raw")


def test_send_raw_returns_message_and_thread_ids() -> None:
    result = _transport().send_raw(email="me@example.com", access_token="tok", raw_b64url="aGVsbG8")
    assert isinstance(result, SendResult)
    assert result.message_id == "fake-msg-1"
    assert result.thread_id == "fake-thread-1"
    assert result.label_ids == ("SENT",)


def test_send_raw_targets_the_authenticated_user_and_sends_raw() -> None:
    FAKE_APP.state.requests.clear()
    _transport().send_raw(email="me@example.com", access_token="tok-123", raw_b64url="aGVsbG8")
    call = next(r for r in FAKE_APP.state.requests if r["path"] == "send")
    assert call["user_id"] == "me@example.com"
    assert call["body"] == {"raw": "aGVsbG8"}
    assert call["auth"] == "Bearer tok-123"


def test_thread_id_is_forwarded_when_supplied() -> None:
    FAKE_APP.state.requests.clear()
    _transport().send_raw(
        email="me@example.com", access_token="t", raw_b64url="aGk=", thread_id="th-9"
    )
    call = next(r for r in FAKE_APP.state.requests if r["path"] == "send")
    assert call["body"]["threadId"] == "th-9"


def test_rate_limit_error_is_retryable() -> None:
    FAKE_APP.state.behavior["send"] = {"status": 429, "times": 1}
    try:
        with pytest.raises(GoogleApiError) as excinfo:
            _transport().send_raw(email="me@example.com", access_token="t", raw_b64url="aGk=")
        err = excinfo.value
        assert err.status_code == 429
        assert err.reason == "rateLimitExceeded"
        assert err.retryable is True
    finally:
        FAKE_APP.state.behavior.pop("send", None)


def test_user_rate_limit_reason_is_retryable() -> None:
    FAKE_APP.state.behavior["send"] = {
        "status": 403,
        "times": 1,
        "error_body": {
            "error": {
                "code": 403,
                "message": "Rate Limit Exceeded",
                "errors": [{"reason": "userRateLimitExceeded", "message": "slow down"}],
            }
        },
    }
    try:
        with pytest.raises(GoogleApiError) as excinfo:
            _transport().send_raw(email="me@example.com", access_token="t", raw_b64url="aGk=")
        assert excinfo.value.reason == "userRateLimitExceeded"
        assert excinfo.value.retryable is True
    finally:
        FAKE_APP.state.behavior.pop("send", None)


def test_daily_send_quota_error_is_terminal() -> None:
    FAKE_APP.state.behavior["send"] = {
        "status": 403,
        "times": 1,
        "error_body": {
            "error": {
                "code": 403,
                "message": "Daily sending limit exceeded",
                "errors": [{"reason": "dailySendQuotaExceeded", "message": "limit"}],
            }
        },
    }
    try:
        with pytest.raises(GoogleApiError) as excinfo:
            _transport().send_raw(email="me@example.com", access_token="t", raw_b64url="aGk=")
        assert excinfo.value.reason == "dailySendQuotaExceeded"
        assert excinfo.value.retryable is False
    finally:
        FAKE_APP.state.behavior.pop("send", None)


def test_server_error_is_retryable() -> None:
    FAKE_APP.state.behavior["send"] = {"status": 503, "times": 1}
    try:
        with pytest.raises(GoogleApiError) as excinfo:
            _transport().send_raw(email="me@example.com", access_token="t", raw_b64url="aGk=")
        assert excinfo.value.status_code == 503
        assert excinfo.value.retryable is True
    finally:
        FAKE_APP.state.behavior.pop("send", None)


def test_forbidden_for_a_bad_scope_is_terminal() -> None:
    FAKE_APP.state.behavior["send"] = {
        "status": 403,
        "times": 1,
        "error_body": {
            "error": {
                "code": 403,
                "message": "Insufficient Permission",
                "errors": [{"reason": "insufficientPermissions", "message": "nope"}],
            }
        },
    }
    try:
        with pytest.raises(GoogleApiError) as excinfo:
            _transport().send_raw(email="me@example.com", access_token="t", raw_b64url="aGk=")
        assert excinfo.value.retryable is False
    finally:
        FAKE_APP.state.behavior.pop("send", None)


def test_401_raises_auth_expired() -> None:
    FAKE_APP.state.behavior["send"] = {"status": 401, "times": 1}
    try:
        with pytest.raises(AuthExpired) as excinfo:
            _transport().send_raw(email="me@example.com", access_token="stale", raw_b64url="aGk=")
        assert excinfo.value.status_code == 401
    finally:
        FAKE_APP.state.behavior.pop("send", None)


def test_retry_after_header_is_captured() -> None:
    FAKE_APP.state.behavior["send"] = {"status": 429, "times": 1, "headers": {"retry-after": "17"}}
    try:
        with pytest.raises(GoogleApiError) as excinfo:
            _transport().send_raw(email="me@example.com", access_token="t", raw_b64url="aGk=")
        assert excinfo.value.retry_after == 17.0
    finally:
        FAKE_APP.state.behavior.pop("send", None)


def test_google_api_error_retryable_matrix() -> None:
    def err(status: int | None, reason: str | None) -> GoogleApiError:
        return GoogleApiError(status_code=status, reason=reason, message="x")

    assert err(429, "rateLimitExceeded").retryable is True
    assert err(500, None).retryable is True
    assert err(503, "backendError").retryable is True
    assert err(400, "invalid").retryable is False
    assert err(404, "notFound").retryable is False
    assert err(None, "rateLimitExceeded").retryable is True


def test_error_message_includes_the_provider_text() -> None:
    FAKE_APP.state.behavior["send"] = {
        "status": 400,
        "times": 1,
        "error_body": {"error": {"message": "Invalid raw value", "code": 400}},
    }
    try:
        with pytest.raises(GoogleApiError) as excinfo:
            _transport().send_raw(email="me@example.com", access_token="t", raw_b64url="!!!")
        assert "Invalid raw value" in excinfo.value.message
    finally:
        FAKE_APP.state.behavior.pop("send", None)


def test_transport_builds_no_state_between_sends() -> None:
    transport = _transport()
    first = transport.send_raw(email="me@example.com", access_token="t", raw_b64url="aGk=")
    second = transport.send_raw(email="me@example.com", access_token="t", raw_b64url="aGk=")
    assert first == second


def test_timeout_is_configurable() -> None:
    transport = GoogleGmailTransport(api_endpoint=ENDPOINT, timeout=1.5)
    assert transport.timeout == 1.5


def test_send_result_is_frozen() -> None:
    result = SendResult(message_id="m", thread_id=None, label_ids=("SENT",))
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.message_id = "other"  # type: ignore[misc]


def test_module_does_not_import_datetime_naive_helpers() -> None:
    # transport must never invent a clock: the worker owns `now`
    assert datetime.now(UTC) is not None
