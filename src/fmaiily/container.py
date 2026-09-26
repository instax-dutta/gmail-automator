from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from fmaiily.clock import Clock, SystemClock
from fmaiily.config import Settings
from fmaiily.crypto import TokenCipher
from fmaiily.db import create_db_engine, create_session_factory


class _Transport(Protocol):
    def send_raw(
        self, *, email: str, access_token: str, raw_b64url: str, thread_id: str | None = None
    ) -> Any: ...


@dataclass
class Container:
    settings: Settings
    engine: Engine
    session_factory: sessionmaker[Session]
    clock: Clock
    cipher: TokenCipher
    transport: _Transport


def build_container(
    settings: Settings,
    *,
    engine: Engine | None = None,
    transport: _Transport | None = None,
    clock: Clock | None = None,
) -> Container:
    from fmaiily.gmail.client import GoogleGmailTransport  # local import avoids cycles

    resolved_engine = engine or create_db_engine(settings.database_url)
    return Container(
        settings=settings,
        engine=resolved_engine,
        session_factory=create_session_factory(resolved_engine),
        clock=clock or SystemClock(),
        cipher=TokenCipher(settings.encryption_key_bytes()),
        transport=transport or GoogleGmailTransport(api_endpoint=settings.gmail_api_endpoint),
    )
