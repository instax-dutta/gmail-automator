from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, PlainTextResponse
from pydantic import BaseModel

from fmaiily import __version__
from fmaiily.container import require
from fmaiily.metrics import Metrics
from fmaiily.rest.deps import ContainerDep

router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    status: str
    version: str
    environment: str
    database: str
    worker_enabled: bool
    oauth_configured: bool
    accounts_connected: int
    queue_depth: int


@router.get("/health", response_model=HealthResponse, summary="Liveness and readiness snapshot")
def health(container: ContainerDep) -> HealthResponse:
    """Unauthenticated on purpose: it exposes no secrets and no addresses.

    Operators and load balancers poll this; agents and humans use `/v1/status` for detail.
    """
    accounts = require(container, "accounts")
    queue = require(container, "queue")
    settings = container.settings
    with container.engine.connect() as conn:
        conn.exec_driver_sql("SELECT 1")
    return HealthResponse(
        status="ok",
        version=__version__,
        environment=settings.environment,
        database=_dialect(container),
        worker_enabled=settings.worker_enabled,
        oauth_configured=settings.is_oauth_configured,
        accounts_connected=len(accounts.list_active()),
        queue_depth=queue.depth(),
    )


@router.get(
    "/metrics",
    response_class=PlainTextResponse,
    summary="Prometheus metrics",
)
def metrics(container: ContainerDep) -> PlainTextResponse:
    """Prometheus exposition for this process.

    Rendered from a registry owned by this container, so several gateway processes on one host do
    not collide (master plan R11). The bind address is loopback by default; if you expose it, keep
    it behind the same control as the send API - these numbers include account addresses.
    """
    registry: Metrics | None = container.metrics
    if registry is None:
        registry = Metrics()
        container.metrics = registry
    return PlainTextResponse(registry.render(), media_type=registry.content_type)


@router.get("/status", response_class=HTMLResponse, summary="Operator status page")
def status_page(container: ContainerDep) -> HTMLResponse:
    """A browser-friendly view of the same numbers as `fmaiily status`.

    Server-rendered with no external assets, so it works on an isolated host with no internet
    access. Unauthenticated like `/health`; it exposes no tokens, only addresses and counts.
    """
    from fmaiily.status_page import collect, render

    return HTMLResponse(render(collect(container)))


def _dialect(container: ContainerDep) -> str:
    return container.engine.dialect.name


__all__ = ["HealthResponse", "health", "router"]
