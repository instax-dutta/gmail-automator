from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from fmaiily.accounts import AccountService
from fmaiily.clock import Clock
from fmaiily.config import Settings
from fmaiily.crypto import TokenCipher
from fmaiily.errors import InvalidRequest, SendFailed
from fmaiily.models import OAuthState


@dataclass(frozen=True)
class AuthorizationRequest:
    authorization_url: str
    state: str
    expires_at: datetime


@dataclass(frozen=True)
class ConnectedAccount:
    email: str
    account_type: str
    scopes: list[str]
    reconnected: bool


class OAuthService:
    """Google OAuth 2.0 authorization-code connect flow.

    The token exchange and the identity lookup are plain RFC 6749 / OpenID Connect calls over an
    injectable `httpx` client. That keeps the whole flow exercisable in-process against the fake
    Google app, with no live network call anywhere in the test suite.

    R2: `gmail.send` cannot read the mailbox profile, so the address comes from the OIDC userinfo
    endpoint, which is why `openid` and `email` are part of the default scope set.
    """

    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        accounts: AccountService,
        cipher: TokenCipher,
        clock: Clock,
        settings: Settings,
        http: httpx.Client | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._accounts = accounts
        self._cipher = cipher
        self._clock = clock
        self._settings = settings
        self._http = http or httpx.Client(timeout=settings.request_timeout_seconds)

    @property
    def settings(self) -> Settings:
        return self._settings

    # ------------------------------------------------------------------- start

    def start(
        self, *, account_hint: str | None = None, redirect_uri: str | None = None
    ) -> AuthorizationRequest:
        client_id, client_secret = self._require_oauth_config()
        now = self._clock.now()
        state = secrets.token_urlsafe(24)
        redirect = redirect_uri or self._settings.oauth_redirect_uri
        expires_at = now + timedelta(seconds=self._settings.oauth_state_ttl_seconds)

        with self._session_factory() as session:
            session.add(
                OAuthState(
                    state=state,
                    redirect_uri=redirect,
                    account_hint=account_hint,
                    created_at=now,
                    expires_at=expires_at,
                )
            )
            session.commit()

        params = {
            "client_id": client_id,
            "redirect_uri": redirect,
            "response_type": "code",
            "scope": " ".join(self._settings.oauth_scopes),
            "access_type": "offline",
            "prompt": "consent",
            "include_granted_scopes": "true",
            "state": state,
        }
        if account_hint:
            params["login_hint"] = account_hint
        separator = "&" if "?" in self._settings.oauth_authorization_uri else "?"
        url = f"{self._settings.oauth_authorization_uri}{separator}{urlencode(params)}"
        _ = client_secret  # the secret never belongs in an authorization URL
        return AuthorizationRequest(authorization_url=url, state=state, expires_at=expires_at)

    # ---------------------------------------------------------------- callback

    def callback(self, *, code: str, state: str) -> ConnectedAccount:
        client_id, client_secret = self._require_oauth_config()
        now = self._clock.now()
        redirect_uri = self._consume_state(state, now=now)

        token_payload = self._exchange_code(
            code=code,
            redirect_uri=redirect_uri,
            client_id=client_id,
            client_secret=client_secret,
        )
        access_token = str(token_payload.get("access_token") or "")
        if not access_token:
            raise SendFailed(
                "token exchange returned no access_token",
                details={"provider": "google"},
            )

        email, verified = self._fetch_identity(access_token)
        if not verified:
            raise InvalidRequest(
                "Google reports this address is not verified; connect a verified account",
                details={"account": email},
            )

        granted = token_payload.get("scope") or ""
        scopes = granted.split() if isinstance(granted, str) else []
        if not scopes:
            scopes = list(self._settings.oauth_scopes)

        expires_in = int(token_payload.get("expires_in") or 3600)
        # store the provider-reported expiry verbatim; TokenManager renews ahead of it
        expiry = now + timedelta(seconds=expires_in)
        refresh_token = token_payload.get("refresh_token")

        already_connected = any(a.email == email for a in self._accounts.list_all())
        self._accounts.upsert_oauth_account(
            email=email,
            account_type=_account_type_for(email),
            access_token_enc=self._cipher.encrypt(access_token, aad=email),
            refresh_token_enc=(
                self._cipher.encrypt(str(refresh_token), aad=email) if refresh_token else None
            ),
            expiry=expiry,
            scopes=scopes,
            token_uri=self._settings.oauth_token_uri,
            now=now,
        )
        return ConnectedAccount(
            email=email,
            account_type=_account_type_for(email),
            scopes=scopes,
            reconnected=already_connected,
        )

    def revoke(self, email: str) -> None:
        self._accounts.revoke(email)

    # ---------------------------------------------------------------- internals

    def _require_oauth_config(self) -> tuple[str, str]:
        client_id = self._settings.google_oauth_client_id
        secret = self._settings.google_oauth_client_secret
        if not client_id or not secret:
            raise InvalidRequest(
                "OAuth is not configured; set FMAIILY_GOOGLE_OAUTH_CLIENT_ID and "
                "FMAIILY_GOOGLE_OAUTH_CLIENT_SECRET",
                details={"account": None},
            )
        return client_id, secret.get_secret_value()

    def _consume_state(self, state: str, *, now: datetime) -> str:
        with self._session_factory() as session:
            row = session.get(OAuthState, state)
            if row is None:
                raise InvalidRequest("unknown OAuth state", details={"state": state})
            if row.consumed_at is not None:
                raise InvalidRequest(
                    "this OAuth state was already used; restart the connect flow",
                    details={"state": state},
                )
            if row.expires_at <= now:
                raise InvalidRequest(
                    "OAuth state expired; restart the connect flow",
                    details={"state": state},
                )
            row.consumed_at = now
            session.commit()
            return row.redirect_uri

    def _exchange_code(
        self, *, code: str, redirect_uri: str, client_id: str, client_secret: str
    ) -> dict[str, Any]:
        try:
            response = self._http.post(
                self._settings.oauth_token_uri,
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": redirect_uri,
                    "client_id": client_id,
                    "client_secret": client_secret,
                },
                headers={"content-type": "application/x-www-form-urlencoded"},
            )
        except httpx.HTTPError as exc:
            raise SendFailed(f"token exchange failed: {exc}") from exc
        if response.status_code >= 400:
            raise SendFailed(
                f"token exchange failed with HTTP {response.status_code}",
                details={"provider": "google", "status": response.status_code},
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise SendFailed("token exchange returned a non-JSON response") from exc
        if not isinstance(payload, dict):
            raise SendFailed("token exchange returned an unexpected payload")
        return payload

    def _fetch_identity(self, access_token: str) -> tuple[str, bool]:
        try:
            response = self._http.get(
                self._settings.oidc_userinfo_url,
                headers={"authorization": f"Bearer {access_token}"},
            )
        except httpx.HTTPError as exc:
            raise SendFailed(f"userinfo lookup failed: {exc}") from exc
        if response.status_code >= 400:
            raise SendFailed(
                f"userinfo lookup failed with HTTP {response.status_code}",
                details={"provider": "google", "status": response.status_code},
            )
        payload = response.json()
        email = str(payload.get("email") or "").strip().lower()
        if not email:
            raise SendFailed(
                "userinfo response contained no email address",
                details={"provider": "google"},
            )
        return email, bool(payload.get("email_verified", True))

    def purge_expired(self) -> int:
        now = self._clock.now()
        with self._session_factory() as session:
            stale = list(session.scalars(select(OAuthState).where(OAuthState.expires_at <= now)))
            for row in stale:
                session.delete(row)
            session.commit()
            return len(stale)


def _account_type_for(email: str) -> str:
    return "personal" if email.endswith("@gmail.com") else "workspace"


__all__ = ["AuthorizationRequest", "ConnectedAccount", "OAuthService"]
