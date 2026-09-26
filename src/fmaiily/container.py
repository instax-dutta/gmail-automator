from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from fmaiily.accounts import AccountService
from fmaiily.clock import Clock, Sleeper, SystemClock
from fmaiily.config import Settings
from fmaiily.crypto import TokenCipher
from fmaiily.db import create_db_engine, create_session_factory
from fmaiily.gmail.client import GoogleGmailTransport, SendResult
from fmaiily.history import HistoryService
from fmaiily.oauth import OAuthService
from fmaiily.queue import QueueService
from fmaiily.quota import QuotaService
from fmaiily.send import SendService
from fmaiily.tokens import TokenManager


class _Transport(Protocol):
    """The whole Gmail surface the rest of the gateway is allowed to see."""

    def send_raw(
        self, *, email: str, access_token: str, raw_b64url: str, thread_id: str | None = None
    ) -> SendResult: ...


@dataclass
class Container:
    """Every long-lived collaborator, wired once at startup and shared by all entry points.

    Construction order follows the dependency graph: clock and cipher are leaves, storage comes
    next, then the domain services, and the Gmail transport last because it is the only piece
    that touches the network.
    """

    settings: Settings
    engine: Engine
    session_factory: sessionmaker[Session]
    clock: Clock
    cipher: TokenCipher
    transport: _Transport
    sleeper: Sleeper | None = None
    http: httpx.Client | None = None
    accounts: AccountService | None = None
    quota: QuotaService | None = None
    queue: QueueService | None = None
    tokens: TokenManager | None = None
    oauth: OAuthService | None = None
    history: HistoryService | None = None
    sender: SendService | None = None
    metrics: Any | None = None
    key_limiter: Any | None = None
    extras: dict[str, Any] = field(default_factory=dict)


def build_container(
    settings: Settings,
    *,
    engine: Engine | None = None,
    transport: _Transport | None = None,
    clock: Clock | None = None,
    sleeper: Sleeper | None = None,
    http: httpx.Client | None = None,
) -> Container:
    resolved_engine = engine or create_db_engine(settings.database_url)
    session_factory = create_session_factory(resolved_engine)
    resolved_clock: Clock = clock or SystemClock()
    cipher = TokenCipher(settings.encryption_key_bytes())
    http_client = http or httpx.Client(timeout=settings.request_timeout_seconds)

    accounts = AccountService(
        session_factory=session_factory, clock=resolved_clock, settings=settings
    )
    quota = QuotaService(session_factory=session_factory, clock=resolved_clock)
    queue = QueueService(
        session_factory=session_factory,
        cipher=cipher,
        clock=resolved_clock,
        settings=settings,
        sleeper=sleeper,
    )
    tokens = TokenManager(
        session_factory=session_factory,
        accounts=accounts,
        cipher=cipher,
        clock=resolved_clock,
        settings=settings,
        http=http_client,
    )
    oauth = OAuthService(
        session_factory=session_factory,
        accounts=accounts,
        cipher=cipher,
        clock=resolved_clock,
        settings=settings,
        http=http_client,
    )
    history = HistoryService(session_factory=session_factory, clock=resolved_clock)
    sender = SendService(
        settings=settings,
        accounts=accounts,
        quota=quota,
        queue=queue,
        clock=resolved_clock,
        sleeper=sleeper,
    )

    return Container(
        settings=settings,
        engine=resolved_engine,
        session_factory=session_factory,
        clock=resolved_clock,
        cipher=cipher,
        transport=transport
        or GoogleGmailTransport(
            api_endpoint=settings.gmail_api_endpoint,
            timeout=settings.request_timeout_seconds,
        ),
        sleeper=sleeper,
        http=http_client,
        accounts=accounts,
        quota=quota,
        queue=queue,
        tokens=tokens,
        oauth=oauth,
        history=history,
        sender=sender,
    )


def require(container: Container, name: str) -> Any:
    """Fetch a wired service, failing loudly if the container was assembled by hand."""
    value = getattr(container, name, None)
    if value is None:
        raise RuntimeError(
            f"container.{name} is not wired; build the container with build_container()"
        )
    return value


__all__ = ["Container", "build_container", "require"]
