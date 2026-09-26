from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from fmaiily.api_keys import ApiKeyContext
from fmaiily.container import require
from fmaiily.errors import AccountNotFound, Forbidden
from fmaiily.history import HistoryService
from fmaiily.rest.deps import ContainerDep, authenticate, require_read
from fmaiily.schemas import HistoryItem, JobStatusResponse

#: Every /v1 router requires a resolved caller. Routes that need the identity itself
#: declare `CallerDep` too; FastAPI caches the dependency, so it authenticates once.
AUTH = [Depends(authenticate)]
ReadCaller = Annotated[ApiKeyContext, Depends(require_read)]

router = APIRouter(prefix="/v1", tags=["jobs"], dependencies=AUTH)


class HistoryResponse(BaseModel):
    items: list[HistoryItem]


@router.get("/jobs/{job_id}", response_model=JobStatusResponse, summary="Status of one send")
def job_status(container: ContainerDep, caller: ReadCaller, job_id: int) -> JobStatusResponse:
    history: HistoryService = require(container, "history")
    status = history.job_status(job_id)
    if status is None:
        raise AccountNotFound("no such send job", details={"job_id": job_id})
    _guard_account(caller, status.account)
    return status


@router.get("/history", response_model=HistoryResponse, summary="Recent sends, newest first")
def history(
    container: ContainerDep,
    caller: ReadCaller,
    account: str | None = Query(default=None),
    status: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
) -> HistoryResponse:
    history_service: HistoryService = require(container, "history")
    if account is not None:
        _guard_account(caller, account)
    items = history_service.list_recent(account_email=account, status=status, limit=limit)
    return HistoryResponse(items=items)


def _guard_account(caller: ApiKeyContext, account: str) -> None:
    if not caller.allows_account(account):
        raise Forbidden(
            "this API key is not permitted to read that account",
            details={"account": account, "key": caller.name},
        )


__all__ = ["HistoryResponse", "router"]
