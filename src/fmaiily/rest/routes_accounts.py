from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from fmaiily.container import require
from fmaiily.rest.deps import ContainerDep, authenticate
from fmaiily.schemas import AccountSummary

#: Every /v1 router requires a resolved caller. Routes that need the identity itself
#: declare `CallerDep` too; FastAPI caches the dependency, so it authenticates once.
AUTH = [Depends(authenticate)]

router = APIRouter(prefix="/v1/accounts", tags=["accounts"], dependencies=AUTH)


class AccountListResponse(BaseModel):
    accounts: list[AccountSummary]


class RevokeResponse(BaseModel):
    account: str
    status: str


@router.get("", response_model=AccountListResponse, summary="List connected Gmail accounts")
def list_accounts(container: ContainerDep) -> AccountListResponse:
    """Includes revoked accounts so an operator can see what was disconnected."""
    return AccountListResponse(accounts=require(container, "accounts").summaries())


@router.get("/{account}", response_model=AccountSummary, summary="Inspect one account")
def get_account(container: ContainerDep, account: str) -> AccountSummary:
    found = require(container, "accounts").get(account)
    return AccountSummary(
        email=found.email,
        account_type=found.account_type,
        status=found.status,
        scopes=list(found.scopes or []),
        created_at=found.created_at,
        next_send_at=found.next_send_at,
    )


@router.delete("/{account}", response_model=RevokeResponse, summary="Disconnect an account")
def revoke_account(container: ContainerDep, account: str) -> RevokeResponse:
    """Wipes the stored tokens immediately; a send in flight fails closed."""
    require(container, "accounts").revoke(account)
    return RevokeResponse(account=account, status="revoked")


__all__ = ["AccountListResponse", "RevokeResponse", "router"]
