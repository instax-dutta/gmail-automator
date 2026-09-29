"""The mailbox and reply routes over HTTP, on the same policy the MCP tools use.

The point of two front doors is that neither can be a way around a rule, so these assert the
refusals and the auth gate as much as the successes.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from gmail_automator.container import build_container
from gmail_automator.rest.app import create_app
from tests.support.fakes import FakeGmailTransport

READ_SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.modify",
]
SEND_ONLY = ["https://www.googleapis.com/auth/gmail.send"]


@pytest.fixture
def read_app(settings, seeded_engine, sleeper, fake_clock):
    from datetime import timedelta

    transport = FakeGmailTransport()
    container = build_container(
        settings,
        engine=seeded_engine,
        transport=transport,
        clock=fake_clock,
        sleeper=sleeper,
    )
    container.accounts.upsert_oauth_account(
        email="me@example.com",
        access_token_enc=container.cipher.encrypt("tok", aad="me@example.com"),
        refresh_token_enc=container.cipher.encrypt("1//r", aad="me@example.com"),
        expiry=fake_clock.now() + timedelta(hours=1),
        scopes=list(READ_SCOPES),
        token_uri="http://oauth.test/token",
    )
    app = create_app(container, settings=settings, start_worker=False)
    with TestClient(app) as client:
        yield client, container, transport


def test_list_messages_over_http(read_app) -> None:
    client, _container, transport = read_app
    transport.seed_message("m1", subject="Hello", snippet="hi")
    response = client.get("/v1/mailbox/messages", params={"query": "is:unread"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["messages"][0]["id"] == "m1"
    assert body["messages"][0]["snippet"] == "hi"


def test_read_message_returns_the_body(read_app) -> None:
    client, _container, transport = read_app
    transport.seed_message("m1", body="hello there")
    response = client.get("/v1/mailbox/messages/m1")
    assert response.status_code == 200, response.text
    assert response.json()["body_text"].strip() == "hello there"


def test_modify_message_over_http(read_app) -> None:
    client, _container, transport = read_app
    transport.seed_message("m1")
    response = client.post("/v1/mailbox/messages/m1/labels", params={"states": "read"})
    assert response.status_code == 200, response.text
    assert "UNREAD" not in response.json()["label_ids"]


def test_list_labels_over_http(read_app) -> None:
    client, _container, transport = read_app
    from gmail_automator.gmail.client import LabelInfo

    transport.labels = (
        LabelInfo(id="Label_1", name="Work", type="user", messages_total=3, messages_unread=1),
    )
    response = client.get("/v1/mailbox/labels")
    assert response.status_code == 200, response.text
    assert response.json()["labels"][0]["name"] == "Work"


def test_a_send_only_account_is_refused_the_same_way_over_http(
    settings, seeded_engine, sleeper, fake_clock
) -> None:
    from datetime import timedelta

    transport = FakeGmailTransport()
    container = build_container(
        settings,
        engine=seeded_engine,
        transport=transport,
        clock=fake_clock,
        sleeper=sleeper,
    )
    container.accounts.upsert_oauth_account(
        email="me@example.com",
        access_token_enc=container.cipher.encrypt("tok", aad="me@example.com"),
        refresh_token_enc=container.cipher.encrypt("1//r", aad="me@example.com"),
        expiry=fake_clock.now() + timedelta(hours=1),
        scopes=list(SEND_ONLY),
        token_uri="http://oauth.test/token",
    )
    with TestClient(create_app(container, settings=settings, start_worker=False)) as client:
        response = client.get("/v1/mailbox/messages")
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "scope_missing"
    assert transport.calls == []


def test_the_mailbox_routes_require_the_api_key(http_settings, seeded_engine, sleeper, fake_clock):
    """The OAuth callback is the only unauthenticated /v1 route; the mailbox must not be one."""
    import secrets

    from pydantic import SecretStr

    # Generated rather than written down: a key literal in a public test suite is a value someone
    # could paste into FMAIL_AUTOMATOR_BOOTSTRAP_ADMIN_KEY and have it authenticate.
    admin_key = f"fmg_{secrets.token_hex(4)}_{secrets.token_urlsafe(32)}"
    tuned = http_settings.model_copy(
        update={"auth_mode": "api_key", "bootstrap_admin_key": SecretStr(admin_key)}
    )
    transport = FakeGmailTransport()
    container = build_container(
        tuned,
        engine=seeded_engine,
        transport=transport,
        clock=fake_clock,
        sleeper=sleeper,
    )
    with TestClient(create_app(container, settings=tuned, start_worker=False)) as client:
        assert client.get("/v1/mailbox/messages").status_code == 401
        assert client.post("/v1/reply", json={"body": "x", "message_id": "m"}).status_code == 401
        # The Google callback stays open, or the consent flow cannot complete in a browser.
        callback = client.get("/v1/oauth/google/callback", params={"code": "x", "state": "bogus"})
        assert callback.status_code != 401


def test_reply_over_http_reports_kind_and_thread(read_app) -> None:
    client, _container, transport = read_app
    transport.seed_message(
        "inbound-1", thread_id="t-9", subject="Question", sender="them@example.com"
    )
    transport.script_result(message_id="reply-1", thread_id="t-9")
    response = client.post(
        "/v1/reply", json={"body": "answer", "message_id": "inbound-1", "wait": False}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["kind"] == "sent"
    assert body["replied_to"] == "message:inbound-1"
    assert body["thread_id"] == "t-9"


def test_reply_over_http_draft(read_app) -> None:
    client, _container, transport = read_app
    transport.seed_message("inbound-1", subject="Question", sender="them@example.com")
    response = client.post(
        "/v1/reply",
        json={"body": "answer", "message_id": "inbound-1", "draft": True},
    )
    assert response.status_code == 200, response.text
    assert response.json()["kind"] == "draft"
    assert response.json()["draft_id"]


def test_reply_rejects_two_targets(read_app) -> None:
    client, _container, _transport = read_app
    response = client.post("/v1/reply", json={"body": "x", "job_id": 1, "message_id": "m"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"


def test_reply_rejects_an_empty_body(read_app) -> None:
    """A malformed body is a 400 by this project's convention; only query/path keep FastAPI's 422."""
    client, _container, _transport = read_app
    response = client.post("/v1/reply", json={"body": "", "message_id": "m"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"
