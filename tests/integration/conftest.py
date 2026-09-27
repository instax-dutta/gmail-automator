"""Fixtures for the integration layer: a real service graph over a temporary SQLite file."""

from __future__ import annotations

import base64
from typing import Any

import pytest
from pydantic import SecretStr
from sqlalchemy import Engine
from sqlalchemy.orm import sessionmaker

from gmail_automator.accounts import AccountService
from gmail_automator.config import Settings
from gmail_automator.container import Container, build_container
from gmail_automator.crypto import TokenCipher
from gmail_automator.history import HistoryService
from gmail_automator.metrics import Metrics
from gmail_automator.queue import QueueService
from gmail_automator.quota import QuotaService
from gmail_automator.send import SendService
from gmail_automator.tokens import TokenManager
from tests.support.fake_gmail_app import DEFAULT_ACCOUNT, fake_gmail_app
from tests.support.fakes import FakeClock, FakeGmailTransport, RecordingSleeper
from tests.support.sync_asgi import sync_asgi_client

FAKE_APP = fake_gmail_app()
#: Stable handle on the shared fake provider, for fixtures that script its behavior.
FAKE_APP_MARKER = FAKE_APP
SENDER = DEFAULT_ACCOUNT
KEY = base64.urlsafe_b64encode(b"i" * 32).decode()


@pytest.fixture
def google_settings(settings: Settings) -> Settings:
    """Settings pointed at the in-process fake Google provider."""
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
def cipher(google_settings: Settings) -> TokenCipher:
    return TokenCipher(google_settings.encryption_key_bytes())


@pytest.fixture
def accounts(
    google_settings: Settings,
    session_factory: sessionmaker,
    seeded_engine: Engine,
    fake_clock: FakeClock,
) -> AccountService:
    return AccountService(
        session_factory=session_factory, clock=fake_clock, settings=google_settings
    )


@pytest.fixture
def account(accounts: AccountService) -> object:
    return accounts.upsert_oauth_account(
        email=SENDER,
        scopes=["https://www.googleapis.com/auth/gmail.send"],
        token_uri="http://oauth.test/token",
    )


@pytest.fixture
def quota(
    google_settings: Settings, session_factory: sessionmaker, fake_clock: FakeClock
) -> QuotaService:
    return QuotaService(session_factory=session_factory, clock=fake_clock)


@pytest.fixture
def queue(
    google_settings: Settings,
    session_factory: sessionmaker,
    seeded_engine: Engine,
    fake_clock: FakeClock,
    cipher: TokenCipher,
    sleeper: RecordingSleeper,
) -> QueueService:
    return QueueService(
        session_factory=session_factory,
        cipher=cipher,
        clock=fake_clock,
        settings=google_settings,
        sleeper=sleeper,
    )


@pytest.fixture
def history(session_factory: sessionmaker, fake_clock: FakeClock) -> HistoryService:
    return HistoryService(session_factory=session_factory, clock=fake_clock)


@pytest.fixture
def tokens(
    google_settings: Settings,
    session_factory: sessionmaker,
    accounts: AccountService,
    cipher: TokenCipher,
    fake_clock: FakeClock,
) -> TokenManager:
    return TokenManager(
        session_factory=session_factory,
        accounts=accounts,
        cipher=cipher,
        clock=fake_clock,
        settings=google_settings,
        http=sync_asgi_client(FAKE_APP, base_url="http://oauth.test"),
    )


@pytest.fixture
def sender(
    google_settings: Settings,
    accounts: AccountService,
    quota: QuotaService,
    queue: QueueService,
    fake_clock: FakeClock,
    sleeper: RecordingSleeper,
) -> SendService:
    return SendService(
        settings=google_settings,
        accounts=accounts,
        quota=quota,
        queue=queue,
        clock=fake_clock,
        sleeper=sleeper,
    )


@pytest.fixture
def metrics() -> Metrics:
    return Metrics()


@pytest.fixture
def build_wired(
    google_settings: Settings,
    seeded_engine: Engine,
    fake_clock: FakeClock,
    sleeper: RecordingSleeper,
    fake_transport: FakeGmailTransport,
    metrics: Metrics,
):
    """Factory for the full container, so a test can vary settings *before* construction.

    Services capture the settings object they are built with, so changing settings afterwards would
    not reach them - which is exactly the kind of half-wired setup worth avoiding.
    """

    def _build(**overrides: Any) -> Container:
        container = build_container(
            google_settings.model_copy(update=overrides) if overrides else google_settings,
            engine=seeded_engine,
            transport=fake_transport,
            clock=fake_clock,
            sleeper=sleeper,
            http=sync_asgi_client(FAKE_APP, base_url="http://oauth.test"),
        )
        container.metrics = metrics
        return container

    return _build


@pytest.fixture
def wired(build_wired) -> Container:
    """The full container, with metrics attached and Google faked in-process."""
    return build_wired()


@pytest.fixture
def connected(wired: Container) -> str:
    """Complete a real OAuth connect so the account holds genuine encrypted tokens."""
    start = wired.oauth.start()
    return wired.oauth.callback(code="code", state=start.state).email


@pytest.fixture(autouse=True)
def _reset_fake_google() -> None:
    FAKE_APP.state.requests.clear()
    FAKE_APP.state.behavior.clear()
    yield
    FAKE_APP.state.requests.clear()
    FAKE_APP.state.behavior.clear()
