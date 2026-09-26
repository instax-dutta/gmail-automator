from __future__ import annotations

import base64
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

import httpx
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from fmaiily.accounts import AccountService
from fmaiily.clock import Clock, Sleeper, SystemClock
from fmaiily.config import Settings
from fmaiily.crypto import TokenCipher
from fmaiily.db import create_db_engine, create_session_factory
from fmaiily.gmail.client import DraftResult, GoogleGmailTransport, SendResult
from fmaiily.history import HistoryService
from fmaiily.metrics import Metrics
from fmaiily.oauth import OAuthService
from fmaiily.queue import QueueService
from fmaiily.quota import QuotaService
from fmaiily.rate_limit import KeyRateLimiter
from fmaiily.send import SendService
from fmaiily.tokens import TokenManager

if TYPE_CHECKING:
    from fmaiily.drafts import DraftService


class _Transport(Protocol):
    """The whole Gmail surface the rest of the gateway is allowed to see."""

    def send_raw(
        self, *, email: str, access_token: str, raw_b64url: str, thread_id: str | None = None
    ) -> SendResult: ...

    def create_draft(
        self, *, email: str, access_token: str, raw_b64url: str, thread_id: str | None = None
    ) -> DraftResult: ...


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
    drafts: DraftService | None = None
    metrics: Metrics | None = None
    key_limiter: KeyRateLimiter | None = None
    extras: dict[str, Any] = field(default_factory=dict)


def _decode_key_base64(value: str) -> bytes:
    raw = value + "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(raw)


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
    old_keys: tuple[bytes, ...] = ()
    if settings.token_encryption_key_old is not None:
        old_keys = (_decode_key_base64(settings.token_encryption_key_old.get_secret_value()),)
    cipher = TokenCipher(settings.encryption_key_bytes(), old_keys=old_keys)
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

    metrics = Metrics()
    key_limiter = KeyRateLimiter(clock=resolved_clock)

    container = Container(
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
        metrics=metrics,
        key_limiter=key_limiter,
    )
    # Attached after construction because a few services need the container they live in.
    from fmaiily.drafts import DraftService

    container.drafts = DraftService(container=container)
    return container


def require(container: Container, name: str) -> Any:
    """Fetch a wired service, failing loudly if the container was assembled by hand."""
    value = getattr(container, name, None)
    if value is None:
        raise RuntimeError(
            f"container.{name} is not wired; build the container with build_container()"
        )
    return value


__all__ = ["Container", "build_container", "require"]
