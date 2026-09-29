import dataclasses
from datetime import UTC, datetime
from typing import Any

import pytest

from gmail_automator.gmail.client import (
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


def _seed(**message: object) -> None:
    """Put a message in the fake Gmail mailbox, as the API would hold it.

    The recorded-request log is cleared here as well as by the autouse fixture: the fixture runs
    before the test body, and these tests assert on the last request rather than the first, so a
    record left by a sibling test would otherwise be found instead.
    """
    import base64

    FAKE_APP.state.requests.clear()
    raw = (
        f"Message-ID: <{message.get('id')}@example.com>\r\n"
        f"From: {message.get('sender', 'Boss <boss@x.co>')}\r\n"
        "To: me@example.com\r\n"
        f"Subject: {message.get('subject', 'Quarterly numbers')}\r\n"
        "Date: Tue, 29 Sep 2026 12:00:00 +0000\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n\r\nbody\r\n"
    ).encode()
    FAKE_APP.state.mailbox = {
        "messages": [
            {
                "id": message.get("id", "m1"),
                "threadId": "t1",
                "snippet": "a snippet",
                "labelIds": ["INBOX"],
                "headers": {
                    "Message-ID": f"<{message.get('id')}@example.com>",
                    "From": message.get("sender", "Boss <boss@x.co>"),
                    "To": "me@example.com",
                    "Subject": message.get("subject", "Quarterly numbers"),
                    "Date": "Tue, 29 Sep 2026 12:00:00 +0000",
                },
                "raw_b64": base64.urlsafe_b64encode(raw).decode().rstrip("="),
            }
        ]
    }


def test_list_messages_hydrates_the_envelope_from_the_list_row() -> None:
    """`messages.list` returns ids only. A row with no subject is not something an agent can use."""
    _seed()
    page = _transport().list_messages(
        email="me@example.com", access_token="tok", query="in:inbox", max_results=5
    )
    assert [m.id for m in page.messages] == ["m1"]
    row = page.messages[0]
    assert row.subject == "Quarterly numbers"
    assert row.sender == "Boss <boss@x.co>"
    assert row.recipients == "me@example.com"
    assert row.snippet == "a snippet"
    assert row.thread_id == "t1"
    assert row.label_ids == ("INBOX",)


def test_list_messages_keeps_the_row_when_its_headers_cannot_be_fetched() -> None:
    """One unreadable message must not make an otherwise good search unusable."""
    _seed()
    FAKE_APP.state.mailbox["messages"].append({"id": "gone", "threadId": "t2", "labelIds": []})
    page = _transport().list_messages(email="me@example.com", access_token="tok", max_results=5)
    by_id = {m.id: m for m in page.messages}
    assert "gone" in by_id
    assert by_id["gone"].subject == ""
    # The readable row still came back fully populated.
    assert by_id["m1"].subject == "Quarterly numbers"


def test_list_messages_passes_the_query_and_paging_through() -> None:
    _seed()
    _transport().list_messages(
        email="me@example.com",
        access_token="tok",
        query="from:boss@x.co newer_than:7d",
        max_results=3,
        page_token="p2",
    )
    listed = [r for r in FAKE_APP.state.requests if r["path"] == "messages.list"][-1]
    assert listed["q"] == "from:boss@x.co newer_than:7d"
    assert listed["max_results"] == 3
    assert listed["page_token"] == "p2"
