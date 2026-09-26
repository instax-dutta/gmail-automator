from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from fmaiily.api_keys import ApiKeyContext
from fmaiily.container import require
from fmaiily.quota import QuotaService, QuotaSnapshot
from fmaiily.rest.deps import ContainerDep, authenticate, require_read
from fmaiily.schemas import QuotaResponse

#: Every /v1 router requires a resolved caller. Routes that need the identity itself
#: declare `CallerDep` too; FastAPI caches the dependency, so it authenticates once.
AUTH = [Depends(authenticate)]
ReadCaller = Annotated[ApiKeyContext, Depends(require_read)]

router = APIRouter(prefix="/v1/quota", tags=["quota"], dependencies=AUTH)


class QuotaListResponse(BaseModel):
    accounts: list[QuotaResponse]


@router.get("", response_model=QuotaListResponse, summary="Quota for every connected account")
def list_quota(container: ContainerDep, _caller: ReadCaller) -> QuotaListResponse:
    quota: QuotaService = require(container, "quota")
    accounts = require(container, "accounts")
    now = container.clock.now()
    return QuotaListResponse(
        accounts=[
            _to_response(quota.snapshot(account, now=now)) for account in accounts.list_active()
        ]
    )


@router.get(
    "/{account}", response_model=QuotaResponse, summary="Remaining 24h capacity for one account"
)
def get_quota(container: ContainerDep, account: str, _caller: ReadCaller) -> QuotaResponse:
    quota: QuotaService = require(container, "quota")
    resolved = require(container, "accounts").resolve(account)
    return _to_response(quota.snapshot(resolved, now=container.clock.now()))


def _to_response(snapshot: QuotaSnapshot) -> QuotaResponse:
    return QuotaResponse(
        account=snapshot.account_email,
        messages_sent=snapshot.messages_sent,
        recipients_sent=snapshot.recipients_sent,
        message_soft_limit=snapshot.message_soft_limit,
        recipient_soft_limit=snapshot.recipient_soft_limit,
        messages_remaining=snapshot.messages_remaining,
        recipients_remaining=snapshot.recipients_remaining,
        pending_jobs=snapshot.pending_jobs,
        queue_depth=snapshot.queue_depth,
        window_hours=snapshot.window_hours,
        reset_at=snapshot.reset_at,
        next_send_at=snapshot.next_send_at,
    )


__all__ = ["QuotaListResponse", "router"]
