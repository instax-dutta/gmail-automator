from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from gmail_automator.container import require
from gmail_automator.oauth import ConnectedAccount
from gmail_automator.rest.deps import CallerDep, ContainerDep, authenticate

#: Every /v1 router requires a resolved caller, declared per route rather than on the router:
#: the callback is the one route a browser reaches without an API key.
AUTH = [Depends(authenticate)]

router = APIRouter(prefix="/v1/oauth/google", tags=["oauth"])


class AuthorizationResponse(BaseModel):
    authorization_url: str
    state: str
    expires_at: str


class ConnectedResponse(BaseModel):
    account: str
    account_type: str
    scopes: list[str]
    reconnected: bool


class DisconnectedResponse(BaseModel):
    account: str
    status: str


@router.get(
    "/start",
    response_model=AuthorizationResponse,
    summary="Begin the Google consent flow",
)
def start(
    container: ContainerDep,
    caller: CallerDep,
    login_hint: str | None = None,
    redirect_uri: str | None = None,
) -> AuthorizationResponse:
    """Return the Google consent URL the operator must open in a browser.

    The gateway itself never opens a browser: it is meant to run headless, so a human completes
    consent and Google redirects to the callback below.
    """
    request = require(container, "oauth").start(account_hint=login_hint, redirect_uri=redirect_uri)
    return AuthorizationResponse(
        authorization_url=request.authorization_url,
        state=request.state,
        expires_at=request.expires_at.isoformat(),
    )


@router.get("/callback", response_model=ConnectedResponse, summary="Google OAuth redirect target")
def callback(container: ContainerDep, code: str, state: str) -> ConnectedResponse:
    """Exchange the authorization code, identify the address, and store encrypted tokens.

    Deliberately unauthenticated: Google redirects the operator's browser here, and a browser
    navigation cannot carry an `Authorization` header. Requiring an API key made the connect flow
    impossible outside `auth_mode=none`. The single-use, expiring `state` is the CSRF protection
    that a bearer token would otherwise have provided - without a valid stored state this route
    exchanges nothing.
    """
    connected: ConnectedAccount = require(container, "oauth").callback(code=code, state=state)
    return ConnectedResponse(
        account=connected.email,
        account_type=connected.account_type,
        scopes=connected.scopes,
        reconnected=connected.reconnected,
    )


@router.delete(
    "/{account}",
    response_model=DisconnectedResponse,
    summary="Disconnect an account and drop its tokens",
)
def disconnect(container: ContainerDep, account: str, caller: CallerDep) -> DisconnectedResponse:
    require(container, "oauth").revoke(account)
    return DisconnectedResponse(account=account, status="revoked")


class StartUrlRequest(BaseModel):
    login_hint: str | None = Field(default=None, max_length=320)
    redirect_uri: str | None = Field(default=None, max_length=500)


__all__ = [
    "AuthorizationResponse",
    "ConnectedResponse",
    "DisconnectedResponse",
    "router",
]
