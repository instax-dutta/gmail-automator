from __future__ import annotations

from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, Header, Request
from sqlalchemy.orm import Session

from gmail_automator.api_keys import SCOPE_READ, ApiKeyContext, bearer_token_from_header
from gmail_automator.container import Container, require
from gmail_automator.errors import GatewayError, Unauthorized


def get_container(request: Request) -> Container:
    container: Container | None = getattr(request.app.state, "container", None)
    if container is None:  # pragma: no cover - only reachable if startup was skipped
        raise RuntimeError("application container is not initialised")
    return container


def get_session(request: Request) -> Iterator[Session]:
    container = get_container(request)
    session = container.session_factory()
    try:
        yield session
    finally:
        session.close()


def authenticate(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> ApiKeyContext:
    """Resolve the caller.

    `auth_mode=none` is for loopback-only local use and yields the fully-permitted local identity.
    Otherwise the bearer token must match the bootstrap admin key or a stored API key. R8.
    """
    container: Container = get_container(request)
    settings = container.settings
    if settings.auth_mode == "none":
        return ApiKeyContext.local()

    token = bearer_token_from_header(authorization)
    bootstrap = settings.bootstrap_admin_key
    if bootstrap is not None and token == bootstrap.get_secret_value():
        return ApiKeyContext(key_id=None, name="bootstrap-admin")

    from gmail_automator.api_keys import ApiKeyService

    try:
        context = ApiKeyService(container=container).verify(token)
    except GatewayError:
        raise
    except Exception as exc:
        raise Unauthorized("API key could not be verified") from exc

    _enforce_key_rate_limit(container, context)
    return context


def _enforce_key_rate_limit(container: Container, context: ApiKeyContext) -> None:
    """PRD 5.3: an optional per-client request cap, on top of Gmail's own limits.

    Only enforced for keys that exist in the database; the bootstrap admin key is the operator's
    own escape hatch and is never rate limited.
    """
    limiter = container.key_limiter
    if limiter is None or context.key_id is None:
        return
    from sqlalchemy import select

    from gmail_automator.models import ApiKeyRow

    with container.session_factory() as session:
        row = session.scalar(select(ApiKeyRow).where(ApiKeyRow.id == context.key_id))
        limit = row.rate_limit_per_minute if row is not None else None
    if limit:
        limiter.check(context.key_id, limit)


ContainerDep = Annotated[Container, Depends(get_container)]
SessionDep = Annotated[Session, Depends(get_session)]
CallerDep = Annotated[ApiKeyContext, Depends(authenticate)]


def require_read(caller: CallerDep) -> ApiKeyContext:
    caller.require_scope(SCOPE_READ)
    return caller


__all__ = [
    "CallerDep",
    "ContainerDep",
    "SessionDep",
    "authenticate",
    "get_container",
    "get_session",
    "require",
    "require_read",
]
