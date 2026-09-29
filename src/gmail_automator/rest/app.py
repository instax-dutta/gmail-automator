from __future__ import annotations

import os
import threading
from collections.abc import AsyncIterator, Awaitable, Callable, MutableMapping
from contextlib import AsyncExitStack, asynccontextmanager, suppress
from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from mcp.server.transport_security import TransportSecuritySettings
from starlette.routing import Match, Mount

from gmail_automator import __version__
from gmail_automator.api_keys import bearer_token_from_header
from gmail_automator.config import Settings
from gmail_automator.container import Container, build_container
from gmail_automator.db import run_migrations
from gmail_automator.errors import GatewayError
from gmail_automator.logging_setup import configure_logging, get_logger
from gmail_automator.rest.errors import error_response, register_exception_handlers
from gmail_automator.worker import Worker

_log = get_logger("gmail_automator.app")

#: Loopback addresses for which `auth_mode=none` is a defensible choice.
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


class _WorkerThread:
    """Owns the send worker thread and its shutdown, so a restart never leaks one."""

    def __init__(self, worker: Worker) -> None:
        self._worker = worker
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._worker.run_forever,
            args=(self._stop,),
            name="gmail_automator-worker",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()
        _log.info("worker_started", worker_id=self._worker.worker_id)

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        self._thread.join(timeout=timeout)


class McpMount(Mount):
    """Serves the MCP endpoint from the root of the app, and guards it with the gateway key.

    Two Starlette facts force this shape:

    * `Mount("/mcp", app=...)` never matches the path `/mcp` itself - only `/mcp/...` - so a
      conventional mount answers every MCP request with a 307 redirect that MCP clients do not
      follow for POST.
    * The MCP server registers its endpoint as an absolute route (`/mcp`), so a mount that does
      match would strip the prefix and the inner router would not find its own route.

    So the mount lives at the root, matches everything, and hands anything outside the MCP prefix
    straight back to the parent app. `_SKIP` stops that hand-back from recursing, which lets the
    normal router produce the correct 404/405 for every other path.

    MCP over stdio is a local process and needs no HTTP auth; this only applies to the Streamable
    HTTP endpoint, which is reachable over the network.
    """

    _SKIP = "gmail_automator.skip_mcp_mount"

    def __init__(
        self,
        *,
        mcp_app: Any,
        parent: Any,
        settings: Settings,
        mount_path: str,
    ) -> None:
        self._mcp_app = mcp_app
        self._parent = parent
        self._settings = settings
        self._path = mount_path.rstrip("/") or "/"
        super().__init__("/", app=mcp_app, name="mcp")

    def matches(self, scope: MutableMapping[str, Any]) -> tuple[Match, MutableMapping[str, Any]]:
        if scope.get(self._SKIP):
            return Match.NONE, {}
        return super().matches(scope)

    async def handle(
        self,
        scope: MutableMapping[str, Any],
        receive: Callable[[], Awaitable[MutableMapping[str, Any]]],
        send: Callable[[MutableMapping[str, Any]], Awaitable[None]],
    ) -> None:
        if scope.get("type") != "http":
            await self._mcp_app(scope, receive, send)
            return
        path = scope.get("path", "")
        if not (path == self._path or path.startswith(f"{self._path}/")):
            scope = {**scope, self._SKIP: True}
            await self._parent(scope, receive, send)
            return
        if self._settings.auth_mode != "none":
            headers = {
                key.decode("latin-1").lower(): value.decode("latin-1")
                for key, value in scope.get("headers", [])
            }
            try:
                bearer_token_from_header(headers.get("authorization"))
            except GatewayError as exc:
                response = error_response(exc.code, exc.message, exc.http_status)
                await response(scope, receive, send)
                return
        await self._mcp_app(scope, receive, send)


def _warn_if_unauthenticated_on_public_interface(settings: Settings) -> None:
    if settings.auth_mode != "none":
        return
    message = "GMAIL_AUTOMATOR_AUTH_MODE=none: the gateway accepts unauthenticated requests"
    if settings.host not in LOOPBACK_HOSTS:
        _log.warning(
            "auth_disabled_on_public_interface",
            host=settings.host,
            warning=message,
            remedy="set GMAIL_AUTOMATOR_AUTH_MODE=api_key before exposing this port",
        )
    else:
        _log.info("auth_disabled_loopback", host=settings.host)


def create_app(
    container: Container | None = None,
    *,
    settings: Settings | None = None,
    run_migrations_on_startup: bool = True,
    start_worker: bool | None = None,
    mount_mcp: bool = True,
) -> FastAPI:
    """Build the ASGI app.

    `container` may be supplied by tests (or by an embedding host) to skip construction; otherwise
    one is built from `settings` during startup so migrations and the worker share the same
    engine. Startup is idempotent: re-entering it replaces the worker thread rather than adding
    a second one.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        candidate: Settings | None = settings or (container.settings if container else None)
        if candidate is None:
            raise RuntimeError("create_app needs either settings or a container")
        configure_logging(level=candidate.log_level, json_output=candidate.log_json)
        _warn_if_unauthenticated_on_public_interface(candidate)

        resolved = container
        if resolved is None:
            if run_migrations_on_startup:
                run_migrations(candidate.database_url, candidate.validate_migrations())
            resolved = build_container(candidate)
        app.state.container = resolved
        _log.info(
            "gateway_starting",
            version=__version__,
            environment=candidate.environment,
            database=candidate.database_url.split("://", 1)[0],
            auth_mode=candidate.auth_mode,
        )

        thread: _WorkerThread | None = None
        should_start = candidate.worker_enabled if start_worker is None else start_worker
        if should_start:
            thread = _WorkerThread(
                Worker(
                    resolved,
                    worker_id=candidate.worker_id or f"worker-{os.getpid()}",
                    poll_interval=candidate.worker_poll_interval_seconds,
                    lease_seconds=candidate.worker_lease_seconds,
                )
            )
            thread.start()

        # Mounting an ASGI app disables its own lifespan, so the MCP session manager has to be
        # entered and held open by the host (master plan R1). `run()` is an async context manager,
        # not a coroutine, and it may only be entered once per server instance.
        mcp_stack = AsyncExitStack()
        session_manager = getattr(app.state, "mcp_session_manager", None)
        if session_manager is not None:
            await mcp_stack.enter_async_context(session_manager.run())

        try:
            yield
        finally:
            if thread is not None:
                thread.stop()
            with suppress(Exception):
                await mcp_stack.aclose()
            if resolved.engine is not None:
                resolved.engine.dispose()
            _log.info("gateway_stopped")

    app = FastAPI(
        title="gmail-automator - Gmail agent gateway",
        version=__version__,
        summary="Send email from your own Gmail accounts, within Gmail's official limits.",
        lifespan=lifespan,
    )
    if container is not None:
        app.state.container = container

    register_exception_handlers(app)
    _mount_routes(app)
    _mount_metrics_middleware(app)

    @app.get("/", include_in_schema=False)
    def root() -> JSONResponse:
        configured: Settings | None = settings or (container.settings if container else None)
        return JSONResponse(
            {
                "service": "gmail_automator",
                "version": __version__,
                "docs": "/docs",
                "rest": "/v1",
                "mcp": configured.mcp_mount_path if configured else None,
            }
        )

    # Mounted last: the MCP shim is mounted at the root, so it must not shadow anything above.
    if mount_mcp:
        _mount_mcp(app, settings or (container.settings if container else None))

    return app


def _mount_metrics_middleware(app: FastAPI) -> None:
    """Count HTTP requests by method, route template, and status.

    The *template* (`/v1/jobs/{job_id}`) is labelled, never the concrete path, so a job id or an
    email address can never become an unbounded metric label.
    """

    @app.middleware("http")
    async def _count(request: Any, call_next: Any) -> Any:
        response = await call_next(request)
        container = getattr(app.state, "container", None)
        registry = getattr(container, "metrics", None) if container else None
        if registry is not None:
            template = _route_template(request)
            registry.record_http_request(request.method, template, response.status_code)
        return response

    def _route_template(request: Any) -> str:
        route = request.scope.get("route")
        path = getattr(route, "path", None)
        return path if isinstance(path, str) else "unmatched"


def _mount_routes(app: FastAPI) -> None:
    from gmail_automator.rest import (
        routes_accounts,
        routes_drafts,
        routes_health,
        routes_jobs,
        routes_oauth,
        routes_quota,
        routes_send,
    )

    app.include_router(routes_health.router)
    app.include_router(routes_oauth.router)
    app.include_router(routes_accounts.router)
    app.include_router(routes_quota.router)
    app.include_router(routes_send.router)
    app.include_router(routes_drafts.router)
    app.include_router(routes_jobs.router)


def _mount_mcp(app: FastAPI, settings: Settings | None) -> None:
    if settings is None:  # pragma: no cover - create_app always has settings by then
        return
    from gmail_automator.mcp_server.server import create_mcp_server

    mcp = create_mcp_server(lambda: _container_of(app))
    http_app = mcp.streamable_http_app(
        stateless_http=True,
        host=settings.host,
        transport_security=_mcp_transport_security(settings),
    )
    app.state.mcp_http_app = http_app
    app.state.mcp_session_manager = mcp.session_manager
    path = settings.mcp_mount_path
    app.router.routes.append(
        McpMount(mcp_app=http_app, parent=app, settings=settings, mount_path=path)
    )
    _log.info("mcp_mounted", path=path)


def _host_pattern(entry: str) -> str:
    """Accept a bare hostname, an explicit port, or a pattern, and produce a pattern.

    The SDK only honours a Host match that is an exact string or ends in ``:*``, so an operator
    writing ``GMAIL_AUTOMATOR_MCP_ALLOWED_HOSTS=mail.example.com`` would silently get 421 unless
    they guessed the wildcard syntax. Normalising here means the obvious input is the right one.
    """
    if entry.endswith(":*"):
        return entry
    if entry.startswith("["):  # IPv6 literal, e.g. [::1]
        return f"{entry}:*" if entry.endswith("]") else entry
    return entry if ":" in entry else f"{entry}:*"


def _mcp_transport_security(settings: Settings) -> TransportSecuritySettings:
    """Build the MCP endpoint's DNS-rebinding allowlist from the real bind address.

    The SDK only auto-enables this check for loopback, and hard-codes loopback hosts when it does.
    A gateway bound to a Tailscale address, a container port, or a reverse proxy therefore answers
    every non-loopback client with 421 - the endpoint is unreachable for anyone but the host it runs
    on. Rather than switching the check off, allowlist what the gateway is actually reachable as:
    its configured bind host, loopback, and anything the operator names.

    A wildcard bind address cannot identify a host, so the operator's list is the only source;
    the gateway logs a warning rather than guessing.
    """
    hosts = [*settings.mcp_allowed_hosts]
    if settings.host not in ("0.0.0.0", "::", ""):
        hosts.append(settings.host)
    hosts += ["127.0.0.1", "localhost", "[::1]"]
    hosts = [_host_pattern(h) for h in hosts]
    if len(hosts) == 3 and not settings.mcp_allowed_hosts:
        _log.warning(
            "mcp_allowed_hosts_unset",
            bind_host=settings.host,
            hint=(
                "bound to a wildcard address, so the MCP endpoint only answers loopback clients. "
                "Set GMAIL_AUTOMATOR_MCP_ALLOWED_HOSTS to the hostname clients use."
            ),
        )
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=hosts,
        allowed_origins=[f"http://{h}" for h in hosts],
    )


def _container_of(app: FastAPI) -> Container:
    container: Container = app.state.container
    return container


def default_settings() -> Settings:
    """Settings from the environment; raises loudly when the encryption key is missing."""
    return Settings()  # GMAIL_AUTOMATOR_TOKEN_ENCRYPTION_KEY is required and validated here


__all__ = [
    "LOOPBACK_HOSTS",
    "McpMount",
    "create_app",
    "default_settings",
]
