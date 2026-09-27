from datetime import timedelta

import httpx
import pytest

from gmail_automator.accounts import AccountService
from gmail_automator.crypto import TokenCipher
from gmail_automator.errors import SendFailed
from gmail_automator.models import Account
from gmail_automator.tokens import TokenManager
from tests.support.fake_gmail_app import fake_gmail_app
from tests.support.sync_asgi import sync_asgi_client

FAKE_APP = fake_gmail_app()
CLIENT_ID = "test-client-id.apps.googleusercontent.com"
CLIENT_SECRET = "GOCSPX-test-secret"
EMAIL = "sender@example.com"


@pytest.fixture
def token_settings(settings):
    from pydantic import SecretStr

    return settings.model_copy(
        update={
            "google_oauth_client_id": CLIENT_ID,
            "google_oauth_client_secret": SecretStr(CLIENT_SECRET),
            "oauth_token_uri": "http://oauth.test/token",
        }
    )


@pytest.fixture
def http() -> httpx.Client:
    return sync_asgi_client(FAKE_APP, base_url="http://oauth.test")


@pytest.fixture
def cipher(token_settings) -> TokenCipher:
    return TokenCipher(token_settings.encryption_key_bytes())


@pytest.fixture
def accounts(session_factory, seeded_engine, fake_clock, token_settings) -> AccountService:
    return AccountService(
        session_factory=session_factory, clock=fake_clock, settings=token_settings
    )


@pytest.fixture
def tokens(session_factory, seeded_engine, fake_clock, token_settings, accounts, cipher, http):
    return TokenManager(
        session_factory=session_factory,
        accounts=accounts,
        cipher=cipher,
        clock=fake_clock,
        settings=token_settings,
        http=http,
    )


@pytest.fixture
def connected(accounts: AccountService, cipher: TokenCipher) -> Account:
    return accounts.upsert_oauth_account(
        email=EMAIL,
        access_token_enc=cipher.encrypt("cached-access-token", aad=EMAIL),
        refresh_token_enc=cipher.encrypt("1//stored-refresh", aad=EMAIL),
        expiry=datetime_now() + timedelta(hours=1),
        scopes=["https://www.googleapis.com/auth/gmail.send"],
        token_uri="http://oauth.test/token",
    )


def datetime_now():
    from datetime import UTC, datetime

    return datetime(2026, 9, 26, 12, 0, tzinfo=UTC)


def test_fresh_token_is_returned_from_cache_without_a_network_call(
    tokens: TokenManager, connected: Account
) -> None:
    FAKE_APP.state.requests.clear()
    result = tokens.access_token(EMAIL)
    assert result.token == "cached-access-token"
    assert result.refreshed is False
    assert FAKE_APP.state.requests == []


def test_expiring_token_is_refreshed(
    tokens: TokenManager, connected: Account, fake_clock, accounts: AccountService
) -> None:
    FAKE_APP.state.requests.clear()
    accounts.record_refresh(EMAIL, expiry=fake_clock.now() + timedelta(seconds=30))
    result = tokens.access_token(EMAIL)
    assert result.refreshed is True
    assert result.token == "fake-access-token"
    assert [r["path"] for r in FAKE_APP.state.requests] == ["/token"]
    form = FAKE_APP.state.requests[0]["form"]
    assert form["grant_type"] == "refresh_token"
    assert form["refresh_token"] == "1//stored-refresh"
    assert form["client_id"] == CLIENT_ID


def test_refresh_persists_new_token_and_expiry(
    tokens: TokenManager, connected: Account, accounts: AccountService, cipher, fake_clock
) -> None:
    accounts.expire_token(EMAIL)
    tokens.access_token(EMAIL)
    stored = accounts.get(EMAIL)
    assert cipher.decrypt(stored.access_token_enc, aad=EMAIL) == "fake-access-token"
    assert stored.token_expiry == fake_clock.now() + timedelta(seconds=3600)
    assert stored.status == "active"
    assert stored.last_refresh_error is None


def test_missing_expiry_triggers_a_refresh(
    tokens: TokenManager, connected: Account, accounts: AccountService
) -> None:
    accounts.expire_token(EMAIL)
    assert tokens.access_token(EMAIL).refreshed is True


def test_leeway_window_forces_refresh_before_expiry(
    session_factory, fake_clock, token_settings, accounts, cipher, http, connected
) -> None:
    # token is still valid for 5 more minutes but sits inside a 10 minute refresh leeway
    wide_leeway = token_settings.model_copy(update={"token_refresh_leeway_seconds": 600})
    manager = TokenManager(
        session_factory=session_factory,
        accounts=accounts,
        cipher=cipher,
        clock=fake_clock,
        settings=wide_leeway,
        http=http,
    )
    accounts.record_refresh(
        EMAIL,
        access_token_enc=cipher.encrypt("cached", aad=EMAIL),
        expiry=fake_clock.now() + timedelta(minutes=5),
    )
    assert manager.access_token(EMAIL).refreshed is True


def test_invalidate_forces_the_next_call_to_refresh(
    tokens: TokenManager, connected: Account, accounts: AccountService
) -> None:
    assert tokens.access_token(EMAIL).refreshed is False
    tokens.invalidate(EMAIL)
    assert tokens.access_token(EMAIL).refreshed is True


def test_refresh_failure_marks_the_account_and_keeps_the_old_token(
    tokens: TokenManager, connected: Account, accounts: AccountService, cipher
) -> None:
    FAKE_APP.state.behavior["token"] = {"status": 400}
    try:
        accounts.expire_token(EMAIL)
        with pytest.raises(SendFailed) as excinfo:
            tokens.access_token(EMAIL)
        assert "refresh" in excinfo.value.message.lower()
        stored = accounts.get(EMAIL)
        assert stored.status == "error"
        assert stored.last_refresh_error is not None
        # the still-valid token survives a transient provider failure
        assert cipher.decrypt(stored.access_token_enc, aad=EMAIL) == "cached-access-token"
    finally:
        FAKE_APP.state.behavior.pop("token", None)


def test_account_without_refresh_token_cannot_refresh(
    tokens: TokenManager, connected: Account, session_factory, accounts: AccountService
) -> None:
    accounts.expire_token(EMAIL)
    with session_factory() as session:
        row = session.get(Account, connected.id)
        row.refresh_token_enc = None
        session.commit()
    with pytest.raises(SendFailed) as excinfo:
        tokens.access_token(EMAIL)
    assert "refresh token" in excinfo.value.message


def test_revoked_account_is_refused(
    tokens: TokenManager, connected: Account, accounts: AccountService
) -> None:
    accounts.revoke(EMAIL)
    with pytest.raises(SendFailed) as excinfo:
        tokens.access_token(EMAIL)
    assert "revoked" in excinfo.value.message


def test_google_issued_refresh_token_replaces_the_stored_one(
    tokens: TokenManager, connected: Account, accounts: AccountService, cipher, fake_clock
) -> None:
    FAKE_APP.state.behavior["rotate_refresh_token"] = "1//rotated"
    try:
        accounts.expire_token(EMAIL)
        tokens.access_token(EMAIL)
        stored = accounts.get(EMAIL)
        assert cipher.decrypt(stored.refresh_token_enc, aad=EMAIL) == "1//rotated"
        assert stored.last_refresh_at == fake_clock.now()
    finally:
        FAKE_APP.state.behavior.pop("rotate_refresh_token", None)
