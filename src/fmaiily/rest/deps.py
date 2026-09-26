from __future__ import annotations

from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, Header, Request
from sqlalchemy.orm import Session

from fmaiily.api_keys import SCOPE_READ, ApiKeyContext, bearer_token_from_header
from fmaiily.container import Container, require
from fmaiily.errors import GatewayError, Unauthorized


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

    from fmaiily.api_keys import ApiKeyService

    try:
        return ApiKeyService(container=container).verify(token)
    except GatewayError:
        raise
    except Exception as exc:
        raise Unauthorized("API key could not be verified") from exc


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
