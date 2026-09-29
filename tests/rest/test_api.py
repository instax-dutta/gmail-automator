"""REST surface: status codes, the error envelope, and the auth gate."""

from __future__ import annotations

import base64

import pytest
from fastapi.testclient import TestClient

from gmail_automator.config import Settings
from gmail_automator.container import build_container
from gmail_automator.rest.app import create_app
from tests.rest.conftest import FAKE_APP
from tests.support.sync_asgi import sync_asgi_client


@pytest.fixture
def token_key() -> str:
    """Obviously fake, per the test-fixture rule: no real credential ever enters the suite."""
    return base64.urlsafe_b64encode(b"r" * 32).decode()


def _error(response) -> dict:
    return response.json()["error"]


# ------------------------------------------------------------------- health


def test_health_is_unauthenticated_and_reports_state(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["database"] == "sqlite"
    assert body["accounts_connected"] == 0
    assert body["queue_depth"] == 0
    assert body["oauth_configured"] is True


def test_health_counts_connected_accounts(client: TestClient, connected: str) -> None:
    assert client.get("/health").json()["accounts_connected"] == 1


def test_root_points_at_the_surfaces(client: TestClient) -> None:
    body = client.get("/").json()
    assert body["service"] == "gmail_automator"
    assert body["rest"] == "/v1"
    assert body["mcp"] == "/mcp"


def test_openapi_schema_is_served(client: TestClient) -> None:
    schema = client.get("/openapi.json").json()
    assert "/v1/send" in schema["paths"]
    assert "/v1/quota" in schema["paths"]


# --------------------------------------------------------------------- auth


def test_requests_are_rejected_without_a_key(authed_client) -> None:
    test_client, _ = authed_client
    response = test_client.get("/v1/accounts")
    assert response.status_code == 401
    assert _error(response)["code"] == "unauthorized"


def test_requests_are_rejected_with_a_wrong_key(authed_client) -> None:
    test_client, _ = authed_client
    response = test_client.get(
        "/v1/accounts", headers={"authorization": "Bearer fmg_deadbeef_wrong"}
    )
    assert response.status_code == 401


def test_a_valid_key_is_accepted(authed_client) -> None:
    test_client, key = authed_client
    response = test_client.get("/v1/accounts", headers={"authorization": f"Bearer {key}"})
    assert response.status_code == 200


def test_bootstrap_admin_key_is_accepted(bootstrap_client) -> None:
    test_client, bootstrap_key = bootstrap_client
    response = test_client.get(
        "/v1/accounts",
        headers={"authorization": f"Bearer {bootstrap_key}"},
    )
    assert response.status_code == 200


def test_a_well_formed_key_that_was_never_issued_is_refused(bootstrap_client) -> None:
    """A structurally valid key that the gateway never issued must not authenticate.

    The suite used to hardcode one such literal, and it is worth keeping this test without keeping
    the literal: a valid-format key published in a public test suite is a credential an operator
    could paste into a production env file. This one is generated at runtime instead, so the
    property is pinned and nothing usable is committed.
    """
    import secrets

    test_client, _bootstrap_key = bootstrap_client
    unissued = f"fmg_{secrets.token_hex(4)}_{secrets.token_urlsafe(32)}"
    response = test_client.get("/v1/accounts", headers={"authorization": f"Bearer {unissued}"})
    assert response.status_code == 401


def test_health_stays_open_when_auth_is_on(authed_client) -> None:
    test_client, _ = authed_client
    assert test_client.get("/health").status_code == 200


# ----------------------------------------------------------- error envelope


def test_validation_errors_use_the_envelope(client: TestClient) -> None:
    response = client.post("/v1/send", json={"to": [], "subject": "s", "body": "b"})
    assert response.status_code == 400
    error = _error(response)
    assert error["code"] == "invalid_request"
    assert error["details"]["errors"]


def test_unknown_route_uses_the_envelope(client: TestClient) -> None:
    response = client.get("/v1/nope")
    assert response.status_code == 404
    assert _error(response)["code"] == "not_found"


def test_method_not_allowed_uses_the_envelope(client: TestClient) -> None:
    response = client.get("/v1/send")
    assert response.status_code == 405
    assert _error(response)["code"] == "method_not_allowed"


def test_missing_account_uses_the_envelope(client: TestClient) -> None:
    response = client.get("/v1/quota/nobody@example.com")
    assert response.status_code == 404
    assert _error(response)["code"] == "account_not_found"


def test_unknown_job_uses_the_envelope(client: TestClient, connected: str) -> None:
    response = client.get("/v1/jobs/4242")
    assert response.status_code == 404
    assert _error(response)["code"] == "account_not_found"


def test_send_without_any_account_explains_what_to_do(client: TestClient) -> None:
    response = client.post("/v1/send", json={"to": ["a@example.com"], "subject": "s", "body": "b"})
    assert response.status_code == 404
    assert "OAuth" in _error(response)["message"]


def test_quota_refusal_uses_429(client: TestClient, connected: str, http_container) -> None:
    from gmail_automator.models import SendJob

    account = http_container.accounts.resolve(None)
    now = http_container.clock.now()
    with http_container.session_factory() as session:
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
    response = client.post("/v1/send", json={"to": ["a@example.com"], "subject": "s", "body": "b"})
    assert response.status_code == 429
    error = _error(response)
    assert error["code"] == "quota_exceeded"
    assert error["details"]["resource"] == "messages"
    assert error["details"]["account"] == connected


# ------------------------------------------------------------------- oauth


def test_oauth_start_returns_a_consent_url(client: TestClient) -> None:
    body = client.get("/v1/oauth/google/start", params={"login_hint": "me@example.com"}).json()
    assert body["authorization_url"].startswith("http://oauth.test/authorize?")
    assert body["state"]
    assert client.get("/v1/oauth/google/start").json()["state"] != body["state"]


def test_oauth_callback_connects_the_account(client: TestClient) -> None:
    start = client.get("/v1/oauth/google/start").json()
    body = client.get(
        "/v1/oauth/google/callback", params={"code": "abc", "state": start["state"]}
    ).json()
    assert body["account"] == "sender@example.com"
    assert body["reconnected"] is False
    assert "gmail.send" in " ".join(body["scopes"])


def test_oauth_callback_rejects_a_replayed_state(client: TestClient) -> None:
    start = client.get("/v1/oauth/google/start").json()
    params = {"code": "abc", "state": start["state"]}
    assert client.get("/v1/oauth/google/callback", params=params).status_code == 200
    replay = client.get("/v1/oauth/google/callback", params=params)
    assert replay.status_code == 400
    assert _error(replay)["code"] == "invalid_request"


def test_oauth_callback_rejects_a_bad_state(client: TestClient) -> None:
    response = client.get("/v1/oauth/google/callback", params={"code": "abc", "state": "nope"})
    assert response.status_code == 400


def test_disconnect_removes_the_account(client: TestClient, connected: str) -> None:
    body = client.request("DELETE", f"/v1/oauth/google/{connected}").json()
    assert body == {"account": connected, "status": "revoked"}
    assert client.get("/v1/accounts").json()["accounts"][0]["status"] == "revoked"


# ---------------------------------------------------------------- accounts


def test_account_listing(client: TestClient, connected: str) -> None:
    accounts = client.get("/v1/accounts").json()["accounts"]
    assert [a["email"] for a in accounts] == [connected]
    assert accounts[0]["status"] == "active"
    assert accounts[0]["scopes"]


def test_account_detail(client: TestClient, connected: str) -> None:
    body = client.get(f"/v1/accounts/{connected}").json()
    assert body["email"] == connected


def test_account_detail_is_case_insensitive(client: TestClient, connected: str) -> None:
    assert client.get(f"/v1/accounts/{connected.upper()}").status_code == 200


def test_account_revoke(client: TestClient, connected: str) -> None:
    assert client.request("DELETE", f"/v1/accounts/{connected}").json()["status"] == "revoked"


# ------------------------------------------------------------------- quota


def test_quota_for_one_account(client: TestClient, connected: str) -> None:
    body = client.get(f"/v1/quota/{connected}").json()
    assert body["account"] == connected
    assert body["messages_remaining"] == 425
    assert body["recipients_remaining"] == 425
    assert body["window_hours"] == 24.0
    assert body["reset_at"] is None


def test_quota_list(client: TestClient, connected: str) -> None:
    assert len(client.get("/v1/quota").json()["accounts"]) == 1


def test_quota_reflects_queued_jobs(client: TestClient, connected: str) -> None:
    client.post(
        "/v1/send",
        json={"to": ["a@example.com"], "subject": "s", "body": "b", "wait": False},
    )
    body = client.get(f"/v1/quota/{connected}").json()
    assert body["pending_jobs"] == 1
    assert body["queue_depth"] == 1
    assert body["messages_remaining"] == 424


# ----------------------------------------------------------------- history


def test_history_and_job_status(client: TestClient, connected: str) -> None:
    queued = client.post(
        "/v1/send",
        json={"to": ["a@example.com"], "subject": "s", "body": "b", "wait": False},
    ).json()
    items = client.get("/v1/history").json()["items"]
    assert [i["job_id"] for i in items] == [queued["job_id"]]
    status = client.get(f"/v1/jobs/{queued['job_id']}").json()
    assert status["status"] == "pending"
    assert status["account"] == connected


def test_history_limit_is_bounded(client: TestClient, connected: str) -> None:
    assert client.get("/v1/history", params={"limit": 0}).status_code == 422
    assert client.get("/v1/history", params={"limit": 5000}).status_code == 422


def test_history_filters_by_account(client: TestClient, connected: str) -> None:
    client.post(
        "/v1/send",
        json={"to": ["a@example.com"], "subject": "s", "body": "b", "wait": False},
    )
    assert len(client.get("/v1/history", params={"account": connected}).json()["items"]) == 1
    assert client.get("/v1/history", params={"account": "other@x.com"}).json()["items"] == []


# -------------------------------------------------------------------- misc


@pytest.mark.parametrize("path", ["/v1/send/limits"])
def test_send_limits_exposes_the_enforced_policy(
    client: TestClient, connected: str, path: str
) -> None:
    body = client.get(path).json()
    assert body["account"] == connected
    assert body["soft_limit_ratio"] == pytest.approx(0.85)
    assert body["max_recipients_per_message"] == 500
    assert body["send_interval_seconds"] == pytest.approx(2.0)


class TestMcpHostAllowlist:
    """The MCP endpoint must answer the host the gateway is actually reached by.

    The SDK auto-enables DNS-rebinding protection with a loopback-only allowlist whenever it is
    not told the real bind address, so a gateway on a Tailscale IP or behind a reverse proxy
    answered every non-loopback client with 421. These tests pin the allowlist to the bind host
    and to whatever the operator declares, and pin that an undeclared host is still refused.

    The bind host here is 192.0.2.10, from RFC 5737 TEST-NET-1, rather than the address of a real
    deployment: a test should not publish someone's infrastructure, and the address is the point
    of the test only in that it is not loopback.
    """

    def _app_for_bind(
        self, http_settings, seeded_engine, fake_clock, sleeper, fake_transport, **kw
    ):
        tuned = http_settings.model_copy(update=kw)
        container = build_container(
            tuned,
            engine=seeded_engine,
            transport=fake_transport,
            clock=fake_clock,
            sleeper=sleeper,
            http=sync_asgi_client(FAKE_APP, base_url="http://oauth.test"),
        )
        return TestClient(create_app(container, settings=tuned, start_worker=False))

    def _post_mcp(self, client: TestClient, host: str | None) -> int:
        headers = {
            "content-type": "application/json",
            "accept": "application/json, text/event-stream",
        }
        if host is not None:
            headers["host"] = host
        return client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers=headers,
        ).status_code

    def test_the_configured_bind_host_is_accepted(
        self, http_settings, seeded_engine, fake_clock, sleeper, fake_transport
    ) -> None:
        client = self._app_for_bind(
            http_settings,
            seeded_engine,
            fake_clock,
            sleeper,
            fake_transport,
            host="192.0.2.10",
            port=8001,
        )
        with client:
            assert self._post_mcp(client, "192.0.2.10:8001") == 200

    def test_loopback_still_works_when_bound_elsewhere(
        self, http_settings, seeded_engine, fake_clock, sleeper, fake_transport
    ) -> None:
        client = self._app_for_bind(
            http_settings,
            seeded_engine,
            fake_clock,
            sleeper,
            fake_transport,
            host="192.0.2.10",
            port=8001,
        )
        with client:
            assert self._post_mcp(client, "127.0.0.1:8001") == 200
            assert self._post_mcp(client, "localhost:8001") == 200

    def test_an_undeclared_host_is_still_refused(
        self, http_settings, seeded_engine, fake_clock, sleeper, fake_transport
    ) -> None:
        """The fix must not become "allow anything": DNS-rebinding protection stays on."""
        client = self._app_for_bind(
            http_settings,
            seeded_engine,
            fake_clock,
            sleeper,
            fake_transport,
            host="192.0.2.10",
            port=8001,
        )
        with client:
            assert self._post_mcp(client, "evil.example.com:8001") == 421

    def test_an_operator_declared_host_is_accepted_for_a_wildcard_bind(
        self, http_settings, seeded_engine, fake_clock, sleeper, fake_transport
    ) -> None:
        """A 0.0.0.0 bind cannot infer its own name, so the reverse-proxy hostname comes from config."""
        client = self._app_for_bind(
            http_settings,
            seeded_engine,
            fake_clock,
            sleeper,
            fake_transport,
            host="0.0.0.0",
            port=8000,
            mcp_allowed_hosts=["mail.example.com"],
        )
        with client:
            assert self._post_mcp(client, "mail.example.com:8000") == 200
            assert self._post_mcp(client, "other.example.com:8000") == 421

    def test_mcp_allowed_hosts_parses_from_a_comma_separated_env_value(
        self, monkeypatch: pytest.MonkeyPatch, token_key: str
    ) -> None:
        monkeypatch.setenv("GMAIL_AUTOMATOR_TOKEN_ENCRYPTION_KEY", token_key)
        monkeypatch.setenv(
            "GMAIL_AUTOMATOR_MCP_ALLOWED_HOSTS", "mail.example.com, chat.example.com"
        )
        assert Settings(_env_file=None).mcp_allowed_hosts == [
            "mail.example.com",
            "chat.example.com",
        ]


class TestOAuthCallbackReachableByABrowser:
    """The callback is the one route a browser reaches with no API key.

    Google redirects the operator's browser straight to `/v1/oauth/google/callback`, and a browser
    navigation cannot set an `Authorization` header. Gating it like every other `/v1` route made
    the connect flow return `401 missing Authorization header` in `auth_mode=api_key` - the mode a
    real deployment runs. The whole REST suite used to run in `auth_mode=none`, which bypasses the
    auth gate, so nothing caught it.
    """

    def test_the_callback_works_with_no_authorization_header(self, authed_client) -> None:
        test_client, key = authed_client
        start = test_client.get(
            "/v1/oauth/google/start", headers={"authorization": f"Bearer {key}"}
        )
        assert start.status_code == 200, start.text
        body = test_client.get(
            "/v1/oauth/google/callback",
            params={"code": "abc", "state": start.json()["state"]},
        )
        assert body.status_code == 200, body.text
        assert body.json()["account"] == "sender@example.com"

    def test_start_still_requires_a_key(self, key_client) -> None:
        test_client, _issue = key_client
        assert test_client.get("/v1/oauth/google/start").status_code == 401

    def test_disconnect_still_requires_a_key(self, key_client) -> None:
        test_client, _issue = key_client
        assert test_client.delete("/v1/oauth/google/sender@example.com").status_code == 401

    def test_a_bad_state_is_rejected_without_a_key(self, key_client) -> None:
        """Unauthenticated does not mean unguarded: without a valid stored state, nothing happens."""
        test_client, _issue = key_client
        response = test_client.get(
            "/v1/oauth/google/callback", params={"code": "abc", "state": "not-a-real-state"}
        )
        assert response.status_code >= 400
        assert response.json()["error"]["code"] != "internal_error"

    def test_a_state_cannot_be_replayed(self, key_client) -> None:
        test_client, issue = key_client
        headers = {"authorization": f"Bearer {issue(name='agent', scopes=('read', 'send'))}"}
        state = test_client.get("/v1/oauth/google/start", headers=headers).json()["state"]
        first = test_client.get("/v1/oauth/google/callback", params={"code": "abc", "state": state})
        assert first.status_code == 200
        again = test_client.get("/v1/oauth/google/callback", params={"code": "abc", "state": state})
        assert again.status_code >= 400
