"""MCP tool surface, driven through the SDK's in-memory client (master plan R1)."""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from fmaiily.container import Container, build_container
from fmaiily.mcp_server.server import create_mcp_server
from mcp import Client
from tests.support.fake_gmail_app import DEFAULT_ACCOUNT, fake_gmail_app
from tests.support.sync_asgi import sync_asgi_client

FAKE_APP = fake_gmail_app()


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def mcp_settings(settings):
    return settings.model_copy(
        update={
            "google_oauth_client_id": "cid",
            "google_oauth_client_secret": SecretStr("csecret"),
            "oauth_authorization_uri": "http://oauth.test/authorize",
            "oauth_token_uri": "http://oauth.test/token",
            "oidc_userinfo_url": "http://oauth.test/v1/userinfo",
        }
    )


@pytest.fixture
def mcp_container(mcp_settings, seeded_engine, fake_clock, sleeper, fake_transport) -> Container:
    return build_container(
        mcp_settings,
        engine=seeded_engine,
        transport=fake_transport,
        clock=fake_clock,
        sleeper=sleeper,
        http=sync_asgi_client(FAKE_APP, base_url="http://oauth.test"),
    )


@pytest.fixture
def server(mcp_container: Container):
    return create_mcp_server(lambda: mcp_container)


@pytest.fixture
def connected(mcp_container: Container) -> str:
    start = mcp_container.oauth.start()
    return mcp_container.oauth.callback(code="code", state=start.state).email


def _structured(result) -> dict:
    """The SDK returns `structured_content` for tools with a declared output model."""
    return result.structured_content


def _tool_error(result) -> str:
    """A `ToolError` on the server comes back as an error result, not a raised exception.

    Returning the message as tool output (rather than failing the JSON-RPC call) is what lets an
    agent read the error code and try something else.
    """
    assert result.is_error, f"expected a tool error, got {result.structured_content}"
    text = "".join(block.text for block in result.content if getattr(block, "type", "") == "text")
    assert text, "an error result must carry readable text"
    return text


@pytest.mark.anyio
async def test_every_expected_tool_is_registered(server) -> None:
    async with Client(server, raise_exceptions=True) as client:
        names = {tool.name for tool in (await client.list_tools()).tools}
    assert names == {
        "send_email",
        "send_batch",
        "get_quota_status",
        "list_accounts",
        "start_account_connect",
        "disconnect_account",
        "get_send_history",
        "get_send_status",
    }


@pytest.mark.anyio
async def test_send_email_schema_documents_the_agent_facing_fields(server) -> None:
    async with Client(server, raise_exceptions=True) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
    send = tools["send_email"]
    required = set(send.input_schema.get("required", []))
    assert {"to", "subject", "body"} <= required
    assert {"cc", "bcc", "body_html", "account", "wait", "idempotency_key"} <= set(
        send.input_schema["properties"]
    )
    assert send.description
    assert send.annotations is not None and send.annotations.read_only_hint is False


@pytest.mark.anyio
async def test_read_only_tools_are_annotated(server) -> None:
    async with Client(server, raise_exceptions=True) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
    for name in ("get_quota_status", "list_accounts", "get_send_history"):
        assert tools[name].annotations.read_only_hint is True, name
    assert tools["disconnect_account"].annotations.destructive_hint is True


@pytest.mark.anyio
async def test_send_email_queues_a_job(server, connected: str) -> None:
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "send_email",
            {"to": ["a@example.com"], "subject": "hi", "body": "hello", "wait": False},
        )
    payload = _structured(result)
    assert payload["status"] == "queued"
    assert payload["account"] == connected
    assert payload["job_id"] > 0
    assert payload["recipients"] == 1


@pytest.mark.anyio
async def test_send_email_counts_cc_and_bcc(server, connected: str) -> None:
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "send_email",
            {
                "to": ["a@example.com"],
                "cc": ["b@example.com"],
                "bcc": ["c@example.com"],
                "subject": "s",
                "body": "b",
                "wait": False,
            },
        )
    assert _structured(result)["recipients"] == 3


@pytest.mark.anyio
async def test_send_email_uses_the_connected_account_by_default(server, connected: str) -> None:
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "send_email", {"to": ["a@example.com"], "subject": "s", "body": "b", "wait": False}
        )
    assert _structured(result)["account"] == connected


@pytest.mark.anyio
async def test_send_email_rejects_a_bad_address(server, connected: str) -> None:
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "send_email", {"to": ["not-an-email"], "subject": "s", "body": "b"}
        )
    assert "invalid" in _tool_error(result).lower()


@pytest.mark.anyio
async def test_quota_exhaustion_surfaces_as_a_tool_error(
    server, mcp_container, connected: str
) -> None:
    from fmaiily.models import SendJob

    account = mcp_container.accounts.resolve(None)
    now = mcp_container.clock.now()
    with mcp_container.session_factory() as session:
        for _ in range(425):
            session.add(
                SendJob(
                    account_id=account.id,
                    status="sent",
                    recipients=1,
                    source="api",
                    scheduled_at=now,
                    sent_at=now,
                    created_at=now,
                    updated_at=now,
                )
            )
        session.commit()
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "send_email", {"to": ["a@example.com"], "subject": "s", "body": "b"}
        )
    assert "quota_exceeded" in _tool_error(result)


@pytest.mark.anyio
async def test_send_email_surfaces_a_domain_error_as_a_tool_error(server, mcp_container) -> None:
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "send_email", {"to": ["a@example.com"], "subject": "s", "body": "b"}
        )
    assert "account_not_found" in _tool_error(result)


@pytest.mark.anyio
async def test_send_email_refuses_an_oversized_body(server, connected: str) -> None:
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "send_email",
            {"to": ["a@example.com"], "subject": "s", "body": "x" * 1_100_000},
        )
    assert "invalid_request" in _tool_error(result)


@pytest.mark.anyio
async def test_send_batch_returns_one_result_per_message(server, connected: str) -> None:
    emails = [
        {"to": [f"r{i}@example.com"], "subject": f"s{i}", "body": "b", "wait": False}
        for i in range(3)
    ]
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("send_batch", {"emails": emails})
    payload = _structured(result)
    assert payload["queued"] == 3
    assert payload["failed"] == 0
    assert len(payload["results"]) == 3
    assert len({r["job_id"] for r in payload["results"]}) == 3


@pytest.mark.anyio
async def test_send_batch_validates_everything_before_queueing(server, connected: str) -> None:
    emails = [
        {"to": ["a@example.com"], "subject": "ok", "body": "b", "wait": False},
        {"to": [f"r{i}@example.com" for i in range(600)], "subject": "too many", "body": "b"},
    ]
    async with Client(server, raise_exceptions=True) as client:
        batch_error = await client.call_tool("send_batch", {"emails": emails})
        assert "invalid_request" in _tool_error(batch_error)
        history = await client.call_tool("get_send_history", {})
    assert _structured(history)["items"] == []


@pytest.mark.anyio
async def test_get_quota_status_reports_the_window(server, connected: str) -> None:
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("get_quota_status", {})
    payload = _structured(result)
    assert payload["account"] == connected
    assert payload["message_soft_limit"] == 425
    assert payload["messages_remaining"] == 425
    assert payload["window_hours"] == 24.0
    assert payload["messages_sent"] == 0


@pytest.mark.anyio
async def test_get_quota_status_drops_after_a_send(server, connected: str) -> None:
    async with Client(server, raise_exceptions=True) as client:
        await client.call_tool(
            "send_email", {"to": ["a@example.com"], "subject": "s", "body": "b", "wait": False}
        )
        result = await client.call_tool("get_quota_status", {})
    payload = _structured(result)
    assert payload["pending_jobs"] == 1
    assert payload["messages_remaining"] == 424


@pytest.mark.anyio
async def test_list_accounts(server, connected: str) -> None:
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("list_accounts", {})
    accounts = _structured(result)["accounts"]
    assert [a["email"] for a in accounts] == [connected]
    assert accounts[0]["status"] == "active"


@pytest.mark.anyio
async def test_start_account_connect_returns_a_consent_url(server) -> None:
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("start_account_connect", {"login_hint": "me@x.com"})
    payload = _structured(result)
    assert payload["authorization_url"].startswith("http://oauth.test/authorize?")
    assert payload["state"]


@pytest.mark.anyio
async def test_disconnect_account_revokes_it(server, connected: str) -> None:
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("disconnect_account", {"account": connected})
        accounts = await client.call_tool("list_accounts", {})
    assert _structured(result)["status"] == "revoked"
    assert _structured(accounts)["accounts"][0]["status"] == "revoked"


@pytest.mark.anyio
async def test_get_send_history_lists_queued_jobs(server, connected: str) -> None:
    async with Client(server, raise_exceptions=True) as client:
        await client.call_tool(
            "send_email", {"to": ["a@example.com"], "subject": "s", "body": "b", "wait": False}
        )
        result = await client.call_tool("get_send_history", {"limit": 5})
    items = _structured(result)["items"]
    assert len(items) == 1
    assert items[0]["status"] == "pending"
    assert items[0]["account"] == connected


@pytest.mark.anyio
async def test_get_send_status_returns_the_job(server, connected: str) -> None:
    async with Client(server, raise_exceptions=True) as client:
        queued = await client.call_tool(
            "send_email", {"to": ["a@example.com"], "subject": "s", "body": "b", "wait": False}
        )
        job_id = _structured(queued)["job_id"]
        status = await client.call_tool("get_send_status", {"job_id": job_id})
    payload = _structured(status)
    assert payload["job_id"] == job_id
    assert payload["status"] == "pending"
    assert payload["account"] == connected


@pytest.mark.anyio
async def test_get_send_status_reports_a_missing_job(server, connected: str) -> None:
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("get_send_status", {"job_id": 9999})
    assert "job_not_found" in _tool_error(result)


@pytest.mark.anyio
async def test_duplicate_idempotency_key_is_reported(server, connected: str) -> None:
    async with Client(server, raise_exceptions=True) as client:
        await client.call_tool(
            "send_email",
            {
                "to": ["a@example.com"],
                "subject": "s",
                "body": "b",
                "wait": False,
                "idempotency_key": "abc",
            },
        )
        duplicate = await client.call_tool(
            "send_email",
            {
                "to": ["a@example.com"],
                "subject": "s",
                "body": "b",
                "wait": False,
                "idempotency_key": "abc",
            },
        )
    assert "duplicate_request" in _tool_error(duplicate)


@pytest.mark.anyio
async def test_thread_id_reaches_the_queue(server, mcp_container, connected: str) -> None:
    async with Client(server, raise_exceptions=True) as client:
        queued = await client.call_tool(
            "send_email",
            {
                "to": ["a@example.com"],
                "subject": "s",
                "body": "b",
                "wait": False,
                "thread_id": "th-7",
            },
        )
    job = mcp_container.queue.get(_structured(queued)["job_id"])
    assert job is not None and job.thread_id == "th-7"


@pytest.mark.anyio
async def test_send_email_delivers_end_to_end_through_the_worker(
    server, mcp_container, connected
) -> None:
    """A blocking tool call returns the Gmail message id once the worker has run."""
    from fmaiily.worker import Worker

    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "send_email",
            {"to": ["a@example.com"], "subject": "s", "body": "b", "wait": False},
        )
    job_id = _structured(result)["job_id"]
    worker = Worker(mcp_container, worker_id="w", rand=lambda: 0.0)
    assert worker.run_once().action == "sent"
    assert mcp_container.queue.get(job_id).gmail_message_id == "msg-1"
    assert connected == DEFAULT_ACCOUNT  # the fake provider is the only account in play
