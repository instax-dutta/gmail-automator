from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from fmaiily import __version__
from fmaiily.container import require
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


def _dialect(container: ContainerDep) -> str:
    return container.engine.dialect.name


__all__ = ["HealthResponse", "health", "router"]
