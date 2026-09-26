"""Fixtures for the integration layer: a real service graph over a temporary SQLite file."""

from __future__ import annotations

import base64

import pytest
from pydantic import SecretStr
from sqlalchemy import Engine
from sqlalchemy.orm import sessionmaker

from fmaiily.accounts import AccountService
from fmaiily.config import Settings
from fmaiily.container import Container, build_container
from fmaiily.crypto import TokenCipher
from fmaiily.history import HistoryService
from fmaiily.metrics import Metrics
from fmaiily.queue import QueueService
from fmaiily.quota import QuotaService
from fmaiily.send import SendService
from fmaiily.tokens import TokenManager
from tests.support.fake_gmail_app import DEFAULT_ACCOUNT, fake_gmail_app
from tests.support.fakes import FakeClock, FakeGmailTransport, RecordingSleeper
from tests.support.sync_asgi import sync_asgi_client

FAKE_APP = fake_gmail_app()
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
def wired(
    google_settings: Settings,
    seeded_engine: Engine,
    fake_clock: FakeClock,
    sleeper: RecordingSleeper,
    fake_transport: FakeGmailTransport,
    metrics: Metrics,
) -> Container:
    """The full container, with metrics attached and Google faked in-process."""
    container = build_container(
        google_settings,
        engine=seeded_engine,
        transport=fake_transport,
        clock=fake_clock,
        sleeper=sleeper,
        http=sync_asgi_client(FAKE_APP, base_url="http://oauth.test"),
    )
    container.metrics = metrics
    return container


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
