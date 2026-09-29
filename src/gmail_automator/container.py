from __future__ import annotations

import base64
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

import httpx
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from gmail_automator.accounts import AccountService
from gmail_automator.clock import Clock, Sleeper, SystemClock
from gmail_automator.config import Settings
from gmail_automator.crypto import TokenCipher
from gmail_automator.db import create_db_engine, create_session_factory
from gmail_automator.gmail.client import DraftResult, GoogleGmailTransport, SendResult
from gmail_automator.history import HistoryService
from gmail_automator.metrics import Metrics
from gmail_automator.oauth import OAuthService
from gmail_automator.queue import QueueService
from gmail_automator.quota import QuotaService
from gmail_automator.rate_limit import KeyRateLimiter
from gmail_automator.send import SendService
from gmail_automator.tokens import TokenManager

if TYPE_CHECKING:
    from gmail_automator.drafts import DraftService
    from gmail_automator.mailbox import MailboxService
    from gmail_automator.replies import ReplyService


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
    mailbox: MailboxService | None = None
    replies: ReplyService | None = None
    metrics: Metrics | None = None
    key_limiter: KeyRateLimiter | None = None
    service_account: Any | None = None
    extras: dict[str, Any] = field(default_factory=dict)


def _load_service_account(settings: Settings) -> Any:
    """Read the service-account key at startup, so a bad path fails loudly and immediately."""
    if not settings.is_service_account_configured:
        return None
    from gmail_automator.service_accounts import load_service_account_key

    return load_service_account_key(
        settings.service_account_key_file,  # type: ignore[arg-type]
        subject=settings.service_account_subject,  # type: ignore[arg-type]
    )


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
    token_request: Any | None = None,
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
    service_account = _load_service_account(settings)
    tokens = TokenManager(
        session_factory=session_factory,
        accounts=accounts,
        cipher=cipher,
        clock=resolved_clock,
        settings=settings,
        http=http_client,
        service_account=service_account,
        token_request=token_request,
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
        service_account=service_account,
    )
    # Attached after construction because these services need the container they live in.
    from gmail_automator.drafts import DraftService
    from gmail_automator.mailbox import MailboxService
    from gmail_automator.replies import ReplyService

    container.drafts = DraftService(container=container)
    container.mailbox = MailboxService(container=container)
    container.replies = ReplyService(container=container)
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
