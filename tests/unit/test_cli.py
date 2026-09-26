"""CLI tests, driven through Typer's runner against a real temporary database."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from fmaiily.cli import app
from fmaiily.container import build_container
from fmaiily.gmail.mime import OutgoingMessage
from tests.support.fake_gmail_app import fake_gmail_app
from tests.support.fakes import FakeGmailTransport
from tests.support.sync_asgi import sync_asgi_client

runner = CliRunner()


@pytest.fixture
def cli_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_clock, fake_transport) -> Path:
    """Point the CLI at a throwaway database and the fake Google/Gmail endpoints."""

    import base64

    database = tmp_path / "cli.db"
    monkeypatch.setenv("FMAIILY_TOKEN_ENCRYPTION_KEY", base64.urlsafe_b64encode(b"c" * 32).decode())
    monkeypatch.setenv("FMAIILY_DATABASE_URL", f"sqlite:///{database}")
    monkeypatch.setenv("FMAIILY_AUTH_MODE", "none")
    monkeypatch.setenv("FMAIILY_WORKER_ENABLED", "false")
    monkeypatch.setenv("FMAIILY_GOOGLE_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("FMAIILY_GOOGLE_OAUTH_CLIENT_SECRET", "csecret")
    monkeypatch.setenv("FMAIILY_OAUTH_AUTHORIZATION_URI", "http://oauth.test/authorize")
    monkeypatch.setenv("FMAIILY_OAUTH_TOKEN_URI", "http://oauth.test/token")
    monkeypatch.setenv("FMAIILY_OIDC_USERINFO_URL", "http://oauth.test/v1/userinfo")
    return database


def _invoke(*args: str):
    return runner.invoke(app, list(args))


def _verify_created_key(candidate: str) -> bool:
    """True when `candidate` is a full, well-formed API key rather than a warning line."""
    import re

    return re.fullmatch(r"fmg_[0-9a-f]{8}_[A-Za-z0-9_-]{43}", candidate) is not None


def test_help_lists_every_command() -> None:
    result = _invoke("--help")
    assert result.exit_code == 0
    for command in ("serve", "mcp-stdio", "migrate", "gen-key", "status", "send-test", "keys"):
        assert command in result.output


def test_version(cli_env: Path) -> None:
    result = _invoke("version")
    assert result.exit_code == 0
    assert result.output.strip() == "0.1.0"


def test_gen_key_prints_a_32_byte_base64_key() -> None:
    import base64

    result = _invoke("gen-key")
    assert result.exit_code == 0
    key = result.output.strip().splitlines()[0]
    assert len(base64.urlsafe_b64decode(key + "==")) == 32


def test_migrate_creates_the_schema(cli_env: Path) -> None:
    result = _invoke("migrate")
    assert result.exit_code == 0, result.output
    assert cli_env.exists()
    from sqlalchemy import create_engine, inspect

    tables = set(inspect(create_engine(f"sqlite:///{cli_env}")).get_table_names())
    assert {"accounts", "send_jobs", "api_keys", "oauth_states"} <= tables


def test_migrate_is_idempotent(cli_env: Path) -> None:
    assert _invoke("migrate").exit_code == 0
    assert _invoke("migrate").exit_code == 0


def test_status_without_any_account(cli_env: Path) -> None:
    assert _invoke("migrate").exit_code == 0
    result = _invoke("status")
    assert result.exit_code == 0
    assert "no accounts connected" in result.output


def test_status_json_shape(cli_env: Path) -> None:
    assert _invoke("migrate").exit_code == 0
    result = _invoke("status", "--json")
    payload = json.loads(result.stdout)
    assert payload["accounts"] == []
    assert payload["queue_depth"] == 0
    assert payload["auth_mode"] == "none"


def test_missing_encryption_key_is_a_clean_error(
    cli_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("FMAIILY_TOKEN_ENCRYPTION_KEY")
    result = _invoke("status")
    assert result.exit_code == 2
    assert "configuration error" in result.output


def test_keys_create_list_revoke(cli_env: Path) -> None:
    assert _invoke("migrate").exit_code == 0
    created = _invoke("keys", "create", "agent", "--scopes", "send,read")
    assert created.exit_code == 0, created.output
    secret = [line for line in created.output.splitlines() if line.startswith("fmg_")]
    assert len(secret) == 1
    full_key = secret[0].strip()

    listed = _invoke("keys", "list")
    assert listed.exit_code == 0
    rows = json.loads(listed.stdout)
    assert [row["name"] for row in rows] == ["agent"]
    assert full_key.split("_", 2)[2] not in listed.output

    prefix = "_".join(full_key.split("_", 2)[:2])
    assert _invoke("keys", "revoke", prefix).exit_code == 0
    after = json.loads(_invoke("keys", "list").stdout)
    assert after[0]["revoked_at"] is not None


def test_keys_create_can_restrict_accounts_and_rate(cli_env: Path) -> None:
    assert _invoke("migrate").exit_code == 0
    result = _invoke(
        "keys",
        "create",
        "scoped",
        "--accounts",
        "a@example.com,b@example.com",
        "--rate-limit",
        "60",
    )
    assert result.exit_code == 0
    rows = json.loads(_invoke("keys", "list").stdout)
    assert rows[0]["allowed_accounts"] == ["a@example.com", "b@example.com"]
    assert rows[0]["rate_limit_per_minute"] == 60


def test_keys_revoke_unknown_prefix_fails_cleanly(cli_env: Path) -> None:
    assert _invoke("migrate").exit_code == 0
    result = _invoke("keys", "revoke", "fmg_00000000")
    assert result.exit_code == 1
    assert "invalid_request" in result.output


def test_accounts_list_and_disconnect(cli_env: Path) -> None:
    """Connect through the fake Google app, then inspect and disconnect via the CLI."""
    assert _invoke("migrate").exit_code == 0
    from fmaiily.config import Settings
    from fmaiily.db import Base, create_db_engine

    settings = Settings()  # type: ignore[call-arg]
    engine = create_db_engine(settings.database_url)
    Base.metadata.create_all(engine)
    container = build_container(
        settings,
        engine=engine,
        transport=FakeGmailTransport(),
        clock=None,
        http=sync_asgi_client(fake_gmail_app(), base_url="http://oauth.test"),
    )
    start = container.oauth.start()
    container.oauth.callback(code="code", state=start.state)
    engine.dispose()

    listed = _invoke("accounts", "list")
    assert listed.exit_code == 0
    rows = json.loads(listed.stdout)
    assert [row["email"] for row in rows] == ["sender@example.com"]

    disconnected = _invoke("accounts", "disconnect", "sender@example.com")
    assert disconnected.exit_code == 0
    assert "disconnected" in disconnected.output
    after = json.loads(_invoke("accounts", "list").stdout)
    assert after[0]["status"] == "revoked"


def test_accounts_connect_prints_the_consent_url(cli_env: Path) -> None:
    assert _invoke("migrate").exit_code == 0
    result = _invoke("accounts", "connect", "--login-hint", "me@example.com")
    assert result.exit_code == 0
    assert "http://oauth.test/authorize?" in result.output
    assert "login_hint=me%40example.com" in result.output


def test_status_shows_quota_after_a_send(cli_env: Path, fake_transport) -> None:
    assert _invoke("migrate").exit_code == 0
    from fmaiily.config import Settings
    from fmaiily.db import Base, create_db_engine

    settings = Settings()  # type: ignore[call-arg]
    engine = create_db_engine(settings.database_url)
    Base.metadata.create_all(engine)
    container = build_container(
        settings,
        engine=engine,
        transport=fake_transport,
        clock=None,
        http=sync_asgi_client(fake_gmail_app(), base_url="http://oauth.test"),
    )
    start = container.oauth.start()
    container.oauth.callback(code="code", state=start.state)
    container.sender.send(
        account_email=None,
        msg=OutgoingMessage(
            from_email="sender@example.com", to=("a@example.com",), subject="s", body="b"
        ),
        source="api",
        wait=False,
    )
    engine.dispose()

    payload = json.loads(_invoke("status", "--json").stdout)
    assert payload["queue_depth"] == 1
    assert payload["accounts"][0]["pending_jobs"] == 1
    assert payload["accounts"][0]["messages_remaining"] == 424


def test_send_test_reports_a_missing_account(cli_env: Path) -> None:
    assert _invoke("migrate").exit_code == 0
    result = _invoke("send-test", "a@example.com")
    assert result.exit_code == 1
    assert "account_not_found" in result.output


def test_purge_history(cli_env: Path) -> None:
    assert _invoke("migrate").exit_code == 0
    result = _invoke("purge-history", "--days", "0")
    assert result.exit_code == 0
    assert "removed 0 history rows" in result.output


def test_one_time_secrets_go_to_stdout_and_warnings_to_stderr(cli_env: Path) -> None:
    """A user pipes the key into a variable; the warning must not land in it.

    The README promises that human messages go to stderr, so that
    `fmaiily keys create agent | tail -1` yields the key. `gen-key` already behaves this way;
    `keys create` did not, and would have handed back the warning text instead of the credential.
    """
    assert _invoke("migrate").exit_code == 0

    key_result = _invoke("gen-key")
    assert "Store this now" not in key_result.stdout
    assert "Store this now" in key_result.stderr
    assert key_result.stdout.strip().splitlines()[-1].startswith("Store") is False

    created = _invoke("keys", "create", "agent", "--scopes", "send")
    assert "only time the key is shown" not in created.stdout
    assert "only time the key is shown" in created.stderr
    # The last line of stdout is the credential, so `| tail -1` captures exactly one full key.
    assert created.stdout.strip().splitlines()[-1].startswith("fmg_")
    assert _verify_created_key(created.stdout.strip().splitlines()[-1])
