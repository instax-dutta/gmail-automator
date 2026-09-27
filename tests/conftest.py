"""gmail-automator test suite. Packages (not namespace dirs) so `tests.support` is importable."""

from __future__ import annotations

import base64
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from gmail_automator.config import Settings
from gmail_automator.db import Base, create_db_engine, create_session_factory
from tests.support.fakes import FakeClock, FakeGmailTransport, RecordingSleeper

FAKE_KEY = base64.urlsafe_b64encode(b"t" * 32).decode()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        token_encryption_key=FAKE_KEY,
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        worker_enabled=False,
        auth_mode="none",
        _env_file=None,
    )


@pytest.fixture
def engine(settings: Settings) -> Iterator[Engine]:
    eng = create_db_engine(settings.database_url)
    yield eng
    eng.dispose()


@pytest.fixture
def session_factory(engine: Engine) -> sessionmaker[Session]:
    return create_session_factory(engine)


@pytest.fixture
def seeded_engine(engine: Engine) -> Engine:
    Base.metadata.create_all(engine)
    return engine


@pytest.fixture
def fake_clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def sleeper(fake_clock: FakeClock) -> RecordingSleeper:
    return RecordingSleeper(clock=fake_clock)


@pytest.fixture
def fake_transport() -> FakeGmailTransport:
    return FakeGmailTransport()


@pytest.fixture
def container(settings: Settings, seeded_engine: Engine, fake_transport, fake_clock):
    from gmail_automator.container import build_container

    return build_container(
        settings, engine=seeded_engine, transport=fake_transport, clock=fake_clock
    )
