"""The read/organise/reply tools as an agent meets them over MCP.

An agent only ever sees the tool descriptions and the results, so these assert on those: the tools
exist, they refuse a scope they do not hold, and a reply comes back with the ids a caller needs to
follow it up.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from gmail_automator.container import build_container
from mcp import Client
from tests.support.fakes import FakeClock, FakeGmailTransport

READ_SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.modify",
]
SEND_ONLY = ["https://www.googleapis.com/auth/gmail.send"]


@pytest.fixture
def agent(settings, seeded_engine, sleeper, fake_clock):
    """An MCP server over a container whose account holds the scopes under test."""

    def _build(scopes):
        transport = FakeGmailTransport()
        container = build_container(
            settings,
            engine=seeded_engine,
            transport=transport,
            clock=FakeClock(),
            sleeper=sleeper,
        )
        # A valid future expiry, so the token manager serves the cached access token instead of
        # attempting a refresh against the real Google endpoint.
        container.accounts.upsert_oauth_account(
            email="me@example.com",
            access_token_enc=container.cipher.encrypt("tok", aad="me@example.com"),
            refresh_token_enc=container.cipher.encrypt("1//r", aad="me@example.com"),
            expiry=fake_clock.now() + timedelta(hours=1),
            scopes=list(scopes),
            token_uri="http://oauth.test/token",
        )
        from gmail_automator.mcp_server.server import create_mcp_server

        return create_mcp_server(lambda: container), container, transport

    return _build


@pytest.mark.anyio
async def test_the_full_suite_is_exposed(agent) -> None:
    server, _container, _transport = agent(READ_SCOPES)
    async with Client(server, raise_exceptions=True) as client:
        names = {t.name for t in (await client.list_tools()).tools}
    assert {
        "list_messages",
        "read_message",
        "list_labels",
        "modify_message",
        "reply",
        "send_email",
        "send_batch",
        "create_draft",
        "get_quota_status",
        "list_accounts",
        "get_send_history",
        "get_send_status",
    } <= names


@pytest.mark.anyio
async def test_list_messages_returns_a_page(agent) -> None:
    server, _container, transport = agent(READ_SCOPES)
    transport.seed_message("m1", subject="Hello", snippet="hi there")
    transport.next_page_token = "page-2"
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("list_messages", {"query": "is:unread"})
    payload = result.structured_content or {}
    assert payload["messages"][0]["id"] == "m1"
    assert payload["messages"][0]["snippet"] == "hi there"
    assert payload["next_page_token"] == "page-2"


@pytest.mark.anyio
async def test_read_message_returns_the_body_of_a_plain_message(agent) -> None:
    server, _container, transport = agent(READ_SCOPES)
    transport.seed_message("m1", body="the plain reading")
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("read_message", {"message_id": "m1"})
    assert not result.is_error, result.content
    payload = result.structured_content or {}
    assert "the plain reading" in (payload["body_text"] or "")


@pytest.mark.anyio
async def test_read_message_returns_the_body_of_an_html_only_multipart_message(agent) -> None:
    """The symptom an agent reported: both bodies null for inbound HTML-only Outlook/Exchange mail.

    Those messages carry no plain alternative, so the whole body sits in one `text/html` leaf of a
    `multipart/alternative` tree and nothing sits on the root part at all.
    """
    server, _container, transport = agent(READ_SCOPES)
    transport.seed_message("html-1", raw=_html_only_message())
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("read_message", {"message_id": "html-1"})
    assert not result.is_error, result.content
    payload = result.structured_content or {}
    assert payload["subject"] == "Your invoice is ready"
    assert payload["sender"] == "Billing <billing@nebius.com>"
    assert "Invoice #5521 is attached." in (payload["body_html"] or "")
    # `body_text` is the field the tool tells an agent to read, so it must carry the prose too.
    assert payload["body_text"] is not None
    assert "Your invoice is ready" in payload["body_text"]
    assert "<p>" not in payload["body_text"]


def _html_only_message() -> bytes:
    """`multipart/alternative` with only a `text/html` child, which is what Exchange actually sends."""
    boundary = "----=_Next_000_ABC123"
    return (
        "From: Billing <billing@nebius.com>\r\n"
        "To: ops@example.com\r\n"
        "Subject: Your invoice is ready\r\n"
        "Message-ID: <abc123@nebius.com>\r\n"
        f'Content-Type: multipart/alternative; boundary="{boundary}"\r\n'
        "\r\n"
        f"--{boundary}\r\n"
        "Content-Type: text/html; charset=utf-8\r\n\r\n"
        "<html><body><h1>Your invoice is ready</h1>"
        "<p>Invoice #5521 is attached.</p></body></html>\r\n"
        f"--{boundary}--\r\n"
    ).encode()


@pytest.mark.anyio
async def test_reading_is_refused_on_a_send_only_account(agent) -> None:
    server, _container, _transport = agent(SEND_ONLY)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("list_messages", {})
    assert result.is_error
    assert "scope" in result.content[0].text.lower()


@pytest.mark.anyio
async def test_modify_message_maps_a_friendly_state(agent) -> None:
    server, _container, transport = agent(READ_SCOPES)
    transport.seed_message("m1")
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("modify_message", {"message_id": "m1", "states": ["read"]})
    assert not result.is_error, result.content
    labels = (result.structured_content or {})["label_ids"]
    assert "UNREAD" not in labels


@pytest.mark.anyio
async def test_a_reply_returns_the_job_and_message_ids(agent) -> None:
    server, _container, transport = agent(READ_SCOPES)
    transport.seed_message(
        "inbound-1", subject="Question", sender="them@example.com", thread_id="t-1"
    )
    transport.script_result(message_id="reply-1", thread_id="t-1")
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "reply", {"body": "the answer", "message_id": "inbound-1", "wait": False}
        )
    assert not result.is_error, result.content
    payload = result.structured_content or {}
    assert payload["kind"] == "sent"
    assert payload["replied_to"] == "message:inbound-1"
    assert payload["thread_id"] == "t-1"
    assert payload["job_id"] is not None


@pytest.mark.anyio
async def test_a_draft_reply_is_reported_as_a_draft(agent) -> None:
    server, _container, transport = agent(READ_SCOPES)
    transport.seed_message("inbound-1", subject="Question", sender="them@example.com")
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "reply", {"body": "draft answer", "message_id": "inbound-1", "draft": True}
        )
    payload = result.structured_content or {}
    assert payload["kind"] == "draft"
    assert payload["draft_id"]


@pytest.mark.anyio
async def test_every_new_tool_states_its_precondition(agent) -> None:
    """A tool an agent cannot pre-check is a wasted turn; the scope must be in the description."""
    server, _container, _transport = agent(READ_SCOPES)
    async with Client(server, raise_exceptions=True) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    assert "gmail.modify" in tools["list_messages"].description
    assert "gmail.modify" in tools["modify_message"].description
    assert "read scope" in tools["reply"].description
    assert "list_messages" in tools["reply"].description
    assert "no permanent delete" in tools["modify_message"].description
