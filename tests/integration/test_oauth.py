from datetime import timedelta
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from pydantic import SecretStr

from gmail_automator.accounts import AccountService
from gmail_automator.crypto import CryptoError, TokenCipher
from gmail_automator.errors import InvalidRequest, SendFailed
from gmail_automator.oauth import OAuthService
from tests.support.fake_gmail_app import fake_gmail_app
from tests.support.sync_asgi import sync_asgi_client

FAKE_APP = fake_gmail_app()
CLIENT_ID = "test-client-id.apps.googleusercontent.com"
CLIENT_SECRET = "GOCSPX-test-secret"


@pytest.fixture
def oauth_settings(settings):
    return settings.model_copy(
        update={
            "google_oauth_client_id": CLIENT_ID,
            "google_oauth_client_secret": SecretStr(CLIENT_SECRET),
            "oauth_authorization_uri": "http://oauth.test/authorize",
            "oauth_token_uri": "http://oauth.test/token",
            "oidc_userinfo_url": "http://oauth.test/v1/userinfo",
        }
    )


@pytest.fixture
def http() -> httpx.Client:
    return sync_asgi_client(FAKE_APP, base_url="http://oauth.test")


@pytest.fixture
def cipher(oauth_settings) -> TokenCipher:
    return TokenCipher(oauth_settings.encryption_key_bytes())


@pytest.fixture
def accounts(session_factory, seeded_engine, fake_clock, oauth_settings) -> AccountService:
    return AccountService(
        session_factory=session_factory, clock=fake_clock, settings=oauth_settings
    )


@pytest.fixture
def oauth(session_factory, seeded_engine, fake_clock, oauth_settings, accounts, cipher, http):
    return OAuthService(
        session_factory=session_factory,
        accounts=accounts,
        cipher=cipher,
        clock=fake_clock,
        settings=oauth_settings,
        http=http,
    )


def _params(url: str) -> dict[str, list[str]]:
    return parse_qs(urlparse(url).query)


def test_start_builds_google_authorization_url(oauth: OAuthService) -> None:
    request = oauth.start()
    assert request.authorization_url.startswith("http://oauth.test/authorize?")
    params = _params(request.authorization_url)
    assert params["client_id"] == [CLIENT_ID]
    assert params["response_type"] == ["code"]
    assert params["redirect_uri"] == ["http://localhost:8000/v1/oauth/google/callback"]
    assert params["state"] == [request.state]
    assert params["access_type"] == ["offline"]
    assert params["prompt"] == ["consent"]
    assert "gmail.send" in params["scope"][0]
    assert request.expires_at.tzinfo is not None


def test_start_includes_login_hint(oauth: OAuthService) -> None:
    request = oauth.start(account_hint="me@example.com")
    assert _params(request.authorization_url)["login_hint"] == ["me@example.com"]


def test_start_states_are_unique(oauth: OAuthService) -> None:
    assert oauth.start().state != oauth.start().state


def test_start_requires_configured_oauth(
    session_factory, seeded_engine, fake_clock, accounts, cipher, http, settings
):
    unconfigured = settings.model_copy(
        update={"google_oauth_client_id": None, "google_oauth_client_secret": None}
    )
    service = OAuthService(
        session_factory=session_factory,
        accounts=accounts,
        cipher=cipher,
        clock=fake_clock,
        settings=unconfigured,
        http=http,
    )
    with pytest.raises(InvalidRequest) as excinfo:
        service.start()
    assert "GMAIL_AUTOMATOR_GOOGLE_OAUTH_CLIENT_ID" in excinfo.value.message


def test_callback_connects_account_and_encrypts_tokens(
    oauth: OAuthService, accounts: AccountService, cipher: TokenCipher
) -> None:
    start = oauth.start()
    connected = oauth.callback(code="auth-code", state=start.state)
    assert connected.email == "sender@example.com"
    # sender@example.com is not a gmail.com address, so it is classified as Workspace
    assert connected.account_type == "workspace"
    assert "gmail.send" in " ".join(connected.scopes)

    account = accounts.get("sender@example.com")
    assert account.refresh_token_enc is not None
    assert "fake-refresh-token" not in (account.refresh_token_enc or "")
    # R7: tokens are bound to the account address via AAD
    assert cipher.decrypt(account.refresh_token_enc, aad="sender@example.com") == (
        "fake-refresh-token"
    )
    with pytest.raises(CryptoError):
        cipher.decrypt(account.refresh_token_enc, aad="someone-else@example.com")


def test_callback_sends_the_authorization_code_and_redirect_uri(
    oauth: OAuthService,
) -> None:
    FAKE_APP.state.requests.clear()
    start = oauth.start()
    oauth.callback(code="the-code", state=start.state)
    token_calls = [r for r in FAKE_APP.state.requests if r["path"] == "/token"]
    assert len(token_calls) == 1
    form = token_calls[0]["form"]
    assert form["grant_type"] == "authorization_code"
    assert form["code"] == "the-code"
    assert form["client_id"] == CLIENT_ID
    assert form["redirect_uri"] == "http://localhost:8000/v1/oauth/google/callback"


def test_callback_identifies_account_via_userinfo_not_gmail_profile(
    oauth: OAuthService,
) -> None:
    FAKE_APP.state.requests.clear()
    start = oauth.start()
    oauth.callback(code="c", state=start.state)
    paths = [r["path"] for r in FAKE_APP.state.requests]
    assert "/v1/userinfo" in paths
    assert "send" not in paths
    userinfo = next(r for r in FAKE_APP.state.requests if r["path"] == "/v1/userinfo")
    assert userinfo["auth"] == "Bearer fake-access-token"


def test_callback_rejects_unknown_state(oauth: OAuthService) -> None:
    with pytest.raises(InvalidRequest):
        oauth.callback(code="c", state="never-issued")


def test_callback_rejects_replayed_state(oauth: OAuthService) -> None:
    start = oauth.start()
    oauth.callback(code="c", state=start.state)
    with pytest.raises(InvalidRequest) as excinfo:
        oauth.callback(code="c", state=start.state)
    assert "already" in excinfo.value.message


def test_callback_rejects_expired_state(oauth: OAuthService, fake_clock) -> None:
    start = oauth.start()
    fake_clock.advance(timedelta(seconds=oauth.settings.oauth_state_ttl_seconds + 1))
    with pytest.raises(InvalidRequest) as excinfo:
        oauth.callback(code="c", state=start.state)
    assert "expired" in excinfo.value.message


def test_callback_stores_granted_scopes(oauth: OAuthService) -> None:
    FAKE_APP.state.behavior["token_scopes"] = "openid email"
    try:
        start = oauth.start()
        connected = oauth.callback(code="c", state=start.state)
        assert connected.scopes == ["openid", "email"]
    finally:
        FAKE_APP.state.behavior.pop("token_scopes", None)


def test_callback_handles_workspace_accounts(oauth: OAuthService) -> None:
    FAKE_APP.state.behavior["userinfo_email"] = "agent@acme.co"
    try:
        start = oauth.start()
        connected = oauth.callback(code="c", state=start.state)
        assert connected.email == "agent@acme.co"
        assert connected.account_type == "workspace"
    finally:
        FAKE_APP.state.behavior.pop("userinfo_email", None)


def test_callback_classifies_personal_gmail_accounts(oauth: OAuthService) -> None:
    FAKE_APP.state.behavior["userinfo_email"] = "Someone@gmail.com"
    try:
        start = oauth.start()
        connected = oauth.callback(code="c", state=start.state)
        assert connected.email == "someone@gmail.com"  # normalized to lower case
        assert connected.account_type == "personal"
    finally:
        FAKE_APP.state.behavior.pop("userinfo_email", None)


def test_callback_rejects_unverified_email(oauth: OAuthService) -> None:
    FAKE_APP.state.behavior["userinfo_email_verified"] = False
    try:
        start = oauth.start()
        with pytest.raises(InvalidRequest):
            oauth.callback(code="c", state=start.state)
    finally:
        FAKE_APP.state.behavior.pop("userinfo_email_verified", None)


def test_callback_reports_token_endpoint_failure(oauth: OAuthService) -> None:
    FAKE_APP.state.behavior["token"] = {"status": 400}
    try:
        start = oauth.start()
        with pytest.raises(SendFailed) as excinfo:
            oauth.callback(code="c", state=start.state)
        assert "token exchange" in excinfo.value.message
    finally:
        FAKE_APP.state.behavior.pop("token", None)


def test_callback_keeps_existing_refresh_token_when_google_omits_one(
    oauth: OAuthService, accounts: AccountService
) -> None:
    start = oauth.start()
    oauth.callback(code="c", state=start.state)
    before = accounts.get("sender@example.com").refresh_token_enc

    FAKE_APP.state.behavior["omit_refresh_token"] = True
    try:
        start2 = oauth.start()
        oauth.callback(code="c2", state=start2.state)
    finally:
        FAKE_APP.state.behavior.pop("omit_refresh_token", None)
    assert accounts.get("sender@example.com").refresh_token_enc == before


def test_revoke_disconnects(oauth: OAuthService, accounts: AccountService) -> None:
    start = oauth.start()
    oauth.callback(code="c", state=start.state)
    oauth.revoke("sender@example.com")
    assert accounts.get("sender@example.com").status == "revoked"
    assert accounts.list_active() == []


def test_purge_expired_states(oauth: OAuthService, fake_clock) -> None:
    oauth.start()
    oauth.start()
    assert oauth.purge_expired() == 0
    fake_clock.advance(timedelta(seconds=oauth.settings.oauth_state_ttl_seconds + 1))
    assert oauth.purge_expired() == 2
