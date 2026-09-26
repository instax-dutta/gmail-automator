"""End-to-end: a real server, a real socket, the real worker thread, the real transport.

Only Google is faked, and it is faked in-process over a real HTTP port so the whole request path
- uvicorn, the ASGI middleware, the mounted MCP app, the worker thread - is exercised as an
operator would run it. Marked `e2e` so the default suite stays socket-free.
"""

from __future__ import annotations

import base64
import json
import socket
import threading
import time
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import uvicorn
from pydantic import SecretStr

from fmaiily.container import build_container
from fmaiily.db import run_migrations
from fmaiily.rest.app import create_app
from fmaiily.sync_asgi_server import serve_asgi_in_thread
from tests.support.fake_gmail_app import DEFAULT_ACCOUNT, fake_gmail_app

pytestmark = pytest.mark.e2e

FAKE_GOOGLE = fake_gmail_app()


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_port(port: int, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.25):
                return
        except OSError:
            time.sleep(0.02)
    raise TimeoutError(f"nothing listening on 127.0.0.1:{port}")


class _Stack:
    """A running gateway plus the fake Google provider, both on real ports."""

    def __init__(self) -> None:
        self.gateway_port = _free_port()
        self.google_port = _free_port()
        self.container: Any = None
        self._threads: list[threading.Thread] = []
        self._servers: list[Any] = []

    def start_gateway(self, settings: Any) -> None:
        """Start a gateway against an existing database, building a fresh engine and container."""
        from fmaiily.rest.app import create_app

        self.container = build_container(settings)
        app = create_app(settings=settings)
        config = uvicorn.Config(
            app, host="127.0.0.1", port=self.gateway_port, log_level="warning", lifespan="on"
        )
        server = uvicorn.Server(config)
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        self._threads.append(thread)
        self._servers.append(server)
        _wait_for_port(self.gateway_port)

    def start(self, tmp_path, **overrides: Any) -> Any:
        google_server = uvicorn.Server(
            uvicorn.Config(
                FAKE_GOOGLE, host="127.0.0.1", port=self.google_port, log_level="warning"
            ),
        )
        google_thread = threading.Thread(target=google_server.run, daemon=True)
        google_thread.start()
        self._threads.append(google_thread)
        self._servers.append(google_server)
        _wait_for_port(self.google_port)

        from fmaiily.config import Settings

        settings = Settings(
            token_encryption_key=base64.urlsafe_b64encode(b"e" * 32).decode(),
            database_url=f"sqlite:///{tmp_path / 'e2e.db'}",
            google_oauth_client_id="cid",
            google_oauth_client_secret=SecretStr("csecret"),
            oauth_authorization_uri=f"http://127.0.0.1:{self.google_port}/authorize",
            oauth_token_uri=f"http://127.0.0.1:{self.google_port}/token",
            oidc_userinfo_url=f"http://127.0.0.1:{self.google_port}/v1/userinfo",
            gmail_api_endpoint=f"http://127.0.0.1:{self.google_port}",
            auth_mode="api_key",
            bootstrap_admin_key=SecretStr("e2e-bootstrap-key"),
            worker_enabled=True,
            worker_poll_interval_seconds=0.05,
            default_send_interval_seconds=0.0,
            log_level="WARNING",
            _env_file=None,
        )
        for key, value in overrides.items():
            settings = settings.model_copy(update={key: value})

        run_migrations(settings.database_url, settings.alembic_ini_path)
        self.container = build_container(settings)
        app = create_app(settings=settings)
        config = uvicorn.Config(
            app, host="127.0.0.1", port=self.gateway_port, log_level="warning", lifespan="on"
        )
        server = uvicorn.Server(config)
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        self._threads.append(thread)
        self._servers.append(server)
        _wait_for_port(self.gateway_port)
        return self

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.gateway_port}"

    def stop(self, *, google: bool = True) -> None:
        """Shut the servers down. `google=False` leaves the fake provider up so a second gateway
        can be pointed at the same one."""
        for index, server in enumerate(self._servers):
            if index == 0 and not google:
                continue
            server.should_exit = True
        for index, thread in enumerate(self._threads):
            if index == 0 and not google:
                continue
            thread.join(timeout=10)


@pytest.fixture
def stack(tmp_path) -> Iterator[_Stack]:
    FAKE_GOOGLE.state.requests.clear()
    FAKE_GOOGLE.state.behavior.clear()
    running = _Stack().start(tmp_path)
    try:
        yield running
    finally:
        running.stop()
        FAKE_GOOGLE.state.requests.clear()
        FAKE_GOOGLE.state.behavior.clear()


def _headers() -> dict[str, str]:
    return {"authorization": "Bearer e2e-bootstrap-key"}


def _connect(stack: _Stack) -> str:
    with httpx.Client(base_url=stack.base_url, timeout=10.0) as http:
        start = http.get("/v1/oauth/google/start", headers=_headers()).json()
        connected = http.get(
            "/v1/oauth/google/callback",
            params={"code": "auth-code", "state": start["state"]},
            headers=_headers(),
        )
    assert connected.status_code == 200, connected.text
    return connected.json()["account"]


def test_health_reports_a_live_gateway(stack: _Stack) -> None:
    with httpx.Client(base_url=stack.base_url, timeout=10.0) as http:
        body = http.get("/health").json()
    assert body["status"] == "ok"
    assert body["worker_enabled"] is True
    assert body["oauth_configured"] is True
    assert body["database"] == "sqlite"


def test_full_send_reaches_the_gmail_api(stack: _Stack) -> None:
    account = _connect(stack)
    with httpx.Client(base_url=stack.base_url, timeout=20.0) as http:
        response = http.post(
            "/v1/send",
            json={"to": ["recipient@example.com"], "subject": "hi", "body": "hello"},
            headers=_headers(),
        )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["status"] == "sent"
    assert payload["message_id"] == "fake-msg-1"
    assert payload["account"] == account

    sends = [r for r in FAKE_GOOGLE.state.requests if r["path"] == "send"]
    assert len(sends) == 1
    assert sends[0]["user_id"] == account
    assert sends[0]["auth"] == "Bearer fake-access-token"
    assert sends[0]["body"]["raw"]


def test_tokens_are_never_returned_over_http(stack: _Stack) -> None:
    _connect(stack)
    with httpx.Client(base_url=stack.base_url, timeout=10.0) as http:
        accounts = http.get("/v1/accounts", headers=_headers()).text
        history = http.get("/v1/history", headers=_headers()).text
    for blob in (accounts, history):
        assert "fake-refresh-token" not in blob
        assert "fake-access-token" not in blob
        assert "1//" not in blob


def test_batch_send_is_paced_and_all_delivered(stack: _Stack) -> None:
    _connect(stack)
    emails = [{"to": [f"r{i}@example.com"], "subject": f"s{i}", "body": "b"} for i in range(3)]
    with httpx.Client(base_url=stack.base_url, timeout=20.0) as http:
        response = http.post(
            "/v1/send/batch", json={"emails": emails, "wait": False}, headers=_headers()
        )
    assert response.status_code == 200, response.text
    jobs = response.json()["jobs"]
    assert len(jobs) == 3
    assert all(job["status"] == "queued" for job in jobs)

    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        with httpx.Client(base_url=stack.base_url, timeout=10.0) as http:
            statuses = [
                http.get(f"/v1/jobs/{job['job_id']}", headers=_headers()).json()["status"]
                for job in jobs
            ]
        if all(status == "sent" for status in statuses):
            break
        time.sleep(0.05)
    assert statuses == ["sent", "sent", "sent"]
    assert len([r for r in FAKE_GOOGLE.state.requests if r["path"] == "send"]) == 3


def test_quota_refusal_never_reaches_gmail(stack: _Stack) -> None:
    account = _connect(stack)
    stack.container.accounts  # noqa: B018 - the container is live, mutate through it below
    with stack.container.session_factory() as session:
        from fmaiily.models import Account, SendJob

        row = session.query(Account).filter_by(email=account).one()
        now = stack.container.clock.now()
        for _ in range(425):
            session.add(
                SendJob(
                    account_id=row.id,
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

    with httpx.Client(base_url=stack.base_url, timeout=10.0) as http:
        response = http.post(
            "/v1/send",
            json={"to": ["r@example.com"], "subject": "s", "body": "b"},
            headers=_headers(),
        )
    assert response.status_code == 429
    assert response.json()["error"]["code"] == "quota_exceeded"
    assert [r for r in FAKE_GOOGLE.state.requests if r["path"] == "send"] == []


def test_rate_limited_send_is_retried_then_succeeds(stack: _Stack) -> None:
    _connect(stack)
    FAKE_GOOGLE.state.behavior["send"] = {
        "status": 429,
        "times": 1,
        "headers": {"retry-after": "0"},
    }
    with httpx.Client(base_url=stack.base_url, timeout=30.0) as http:
        response = http.post(
            "/v1/send",
            json={"to": ["r@example.com"], "subject": "s", "body": "b"},
            headers=_headers(),
        )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "sent"
    assert len([r for r in FAKE_GOOGLE.state.requests if r["path"] == "send"]) == 2


def test_daily_quota_error_fails_the_job_without_retrying(stack: _Stack) -> None:
    _connect(stack)
    FAKE_GOOGLE.state.behavior["send"] = {
        "status": 403,
        "times": 5,
        "error_body": {
            "error": {
                "code": 403,
                "message": "Daily sending limit exceeded",
                "errors": [{"reason": "dailySendQuotaExceeded", "message": "limit"}],
            }
        },
    }
    with httpx.Client(base_url=stack.base_url, timeout=30.0) as http:
        response = http.post(
            "/v1/send",
            json={"to": ["r@example.com"], "subject": "s", "body": "b", "wait": False},
            headers=_headers(),
        )
    job_id = response.json()["job_id"]
    deadline = time.monotonic() + 15
    status: dict[str, Any] = {}
    while time.monotonic() < deadline:
        with httpx.Client(base_url=stack.base_url, timeout=10.0) as http:
            status = http.get(f"/v1/jobs/{job_id}", headers=_headers()).json()
        if status["status"] == "failed":
            break
        time.sleep(0.05)
    assert status["status"] == "failed"
    assert status["error_code"] == "daily_send_quota_exceeded"
    assert len([r for r in FAKE_GOOGLE.state.requests if r["path"] == "send"]) == 1


def test_mcp_endpoint_requires_a_key(stack: _Stack) -> None:
    with httpx.Client(base_url=stack.base_url, timeout=10.0) as http:
        response = http.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={
                "content-type": "application/json",
                "accept": "application/json, text/event-stream",
            },
        )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_mcp_endpoint_lists_tools_when_authenticated(stack: _Stack) -> None:
    with httpx.Client(base_url=stack.base_url, timeout=10.0) as http:
        response = http.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={
                **_headers(),
                "content-type": "application/json",
                "accept": "application/json, text/event-stream",
            },
        )
    assert response.status_code == 200, response.text
    assert "send_email" in response.text


def test_a_queued_job_survives_a_gateway_restart(tmp_path) -> None:
    """Durability: a job written by one process is sent by the next one that starts.

    The first instance runs with the worker disabled, so the job is definitely still queued when
    the process goes away. The second instance drains it from the database alone.
    """
    FAKE_GOOGLE.state.requests.clear()
    FAKE_GOOGLE.state.behavior.clear()

    writer = _Stack().start(tmp_path / "writer", worker_enabled=False)
    account = _connect(writer)
    with httpx.Client(base_url=writer.base_url, timeout=20.0) as http:
        queued = http.post(
            "/v1/send",
            json={"to": ["r@example.com"], "subject": "s", "body": "b", "wait": False},
            headers=_headers(),
        ).json()
    assert queued["status"] == "queued"
    assert [r for r in FAKE_GOOGLE.state.requests if r["path"] == "send"] == []
    settings = writer.container.settings
    writer.container.engine.dispose()
    writer.stop(google=False)  # the fake provider stays up for the second process

    reader = _Stack()
    reader.gateway_port = _free_port()
    # same database, same tokens, but this process is allowed to drain the queue
    reader.start_gateway(settings.model_copy(update={"worker_enabled": True}))
    try:
        deadline = time.monotonic() + 20
        status: dict[str, Any] = {}
        while time.monotonic() < deadline:
            with httpx.Client(base_url=reader.base_url, timeout=10.0) as http:
                status = http.get(f"/v1/jobs/{queued['job_id']}", headers=_headers()).json()
            if status.get("status") == "sent":
                break
            time.sleep(0.05)
        assert status.get("status") == "sent", status
        sends = [r for r in FAKE_GOOGLE.state.requests if r["path"] == "send"]
        assert len(sends) == 1
        assert sends[0]["user_id"] == account
    finally:
        reader.stop()
        FAKE_GOOGLE.state.requests.clear()
        FAKE_GOOGLE.state.behavior.clear()


def test_health_stays_up_without_any_account(stack: _Stack) -> None:
    with httpx.Client(base_url=stack.base_url, timeout=10.0) as http:
        assert http.get("/health").json()["accounts_connected"] == 0
        assert json.loads(http.get("/v1/quota", headers=_headers()).text)["accounts"] == []


def test_default_account_is_the_connected_one(stack: _Stack) -> None:
    account = _connect(stack)
    assert account == DEFAULT_ACCOUNT
    with httpx.Client(base_url=stack.base_url, timeout=10.0) as http:
        assert http.get(f"/v1/quota/{account}", headers=_headers()).status_code == 200


def test_async_asgi_helper_serves_the_fake_provider() -> None:
    """The helper used by contract tests starts a real server for an ASGI app."""
    port = _free_port()
    server = serve_asgi_in_thread(FAKE_GOOGLE, port=port)
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=10.0) as http:
            response = http.post(
                "/token", data={"grant_type": "refresh_token", "refresh_token": "r"}
            )
        assert response.status_code == 200
        assert response.json()["access_token"] == "fake-access-token"
    finally:
        server.stop()
