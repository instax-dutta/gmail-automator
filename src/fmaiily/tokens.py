from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import httpx
from sqlalchemy.orm import Session, sessionmaker

from fmaiily.accounts import AccountService
from fmaiily.clock import Clock
from fmaiily.config import Settings
from fmaiily.crypto import TokenCipher
from fmaiily.errors import SendFailed
from fmaiily.service_accounts import (
    SERVICE_ACCOUNT_SCOPES,
    ServiceAccountConfig,
    TokenRequest,
    mint_access_token,
)


@dataclass(frozen=True)
class AccessToken:
    token: str
    expiry: datetime | None
    refreshed: bool


class TokenManager:
    """Keeps a usable Gmail access token available for an account.

    Tokens are short-lived, so the worker asks for one per send. A token inside the refresh
    leeway is renewed proactively; a token that Gmail rejects with 401 is invalidated by
    `invalidate()` and renewed on the following attempt.

    The refresh grant is a plain RFC 6749 form POST over an injectable `httpx` client, mirroring
    `OAuthService`, so the whole lifecycle is testable in-process.
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
        service_account: ServiceAccountConfig | None = None,
        token_request: TokenRequest | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._accounts = accounts
        self._cipher = cipher
        self._clock = clock
        self._settings = settings
        self._http = http or httpx.Client(timeout=settings.request_timeout_seconds)
        self._service_account = service_account
        self._token_request = token_request or _google_request()

    @property
    def settings(self) -> Settings:
        return self._settings

    def access_token(self, email: str, *, now: datetime | None = None) -> AccessToken:
        now = now or self._clock.now()
        account = self._accounts.get(email)
        if account.status == "revoked":
            # Checked before the service-account branch too: revoking must stop sending whether
            # the account authenticates by OAuth or by impersonation.
            raise SendFailed(
                f"account {email} is revoked; reconnect it before sending",
                details={"account": email},
            )
        if account.auth_type == "service_account":
            return self._service_account_token(account.email, now=now)
        if not account.access_token_enc:
            raise SendFailed(
                f"account {email} has no access token; reconnect it",
                details={"account": email},
            )
        if self._needs_refresh(account.token_expiry, now):
            return self.refresh(email, now=now)
        return AccessToken(
            token=self._cipher.decrypt(account.access_token_enc, aad=account.email),
            expiry=account.token_expiry,
            refreshed=False,
        )

    def refresh(self, email: str, *, now: datetime | None = None) -> AccessToken:
        now = now or self._clock.now()
        account = self._accounts.get(email)
        if not account.refresh_token_enc:
            self._accounts.record_refresh(email, error="missing_refresh_token")
            raise SendFailed(
                f"account {email} has no refresh token; reconnect it to restore sending",
                details={"account": email},
            )

        refresh_token = self._cipher.decrypt(account.refresh_token_enc, aad=account.email)
        client_id = self._settings.google_oauth_client_id
        client_secret = self._settings.google_oauth_client_secret
        body = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id or "",
            "client_secret": client_secret.get_secret_value() if client_secret else "",
        }

        try:
            response = self._http.post(
                account.token_uri or self._settings.oauth_token_uri,
                data=body,
                headers={"content-type": "application/x-www-form-urlencoded"},
            )
        except httpx.HTTPError as exc:
            self._accounts.record_refresh(email, error=f"transport_error: {exc}")
            raise SendFailed(
                f"token refresh failed for {email}: {exc}", details={"account": email}
            ) from exc

        if response.status_code >= 400:
            detail = _error_detail(response)
            self._accounts.record_refresh(email, error=f"http_{response.status_code}: {detail}")
            raise SendFailed(
                f"token refresh failed for {email} with HTTP {response.status_code}",
                details={"account": email, "status": response.status_code},
            )

        try:
            payload: Any = response.json()
        except ValueError as exc:
            self._accounts.record_refresh(email, error="non_json_response")
            raise SendFailed(
                f"token refresh for {email} returned a non-JSON response",
                details={"account": email},
            ) from exc

        access_token = str(payload.get("access_token") or "")
        if not access_token:
            self._accounts.record_refresh(email, error="no_access_token")
            raise SendFailed(
                f"token refresh for {email} returned no access_token",
                details={"account": email},
            )

        expires_in = int(payload.get("expires_in") or 3600)
        expiry = now + timedelta(seconds=expires_in)
        rotated = payload.get("refresh_token")
        # Google may rotate the refresh token; the newest one must win.
        self._accounts.record_refresh(
            email,
            access_token_enc=self._cipher.encrypt(access_token, aad=email),
            refresh_token_enc=(self._cipher.encrypt(str(rotated), aad=email) if rotated else None),
            expiry=expiry,
        )
        return AccessToken(token=access_token, expiry=expiry, refreshed=True)

    def _service_account_token(self, email: str, *, now: datetime) -> AccessToken:
        """Mint an access token for an impersonated Workspace mailbox.

        There is no refresh token in this path, so nothing is persisted and nothing can leak; the
        assertion is rebuilt on demand and the result lives only in memory.
        """
        config = self._service_account
        if config is None:
            raise SendFailed(
                f"account {email} is a service account but no service account key is configured",
                details={"account": email},
            )
        if config.subject.lower() != email.lower():
            raise SendFailed(
                f"the configured service account impersonates {config.subject}, not {email}",
                details={"account": email, "subject": config.subject},
            )
        token, expiry = mint_access_token(
            config, request=self._token_request, now=now, scopes=tuple(account_scopes(config))
        )
        self._accounts.record_refresh(email, expiry=expiry)
        return AccessToken(token=token, expiry=expiry, refreshed=True)

    def invalidate(self, email: str) -> None:
        self._accounts.expire_token(email)

    def _needs_refresh(self, expiry: datetime | None, now: datetime) -> bool:
        if expiry is None:
            return True
        leeway = timedelta(seconds=self._settings.token_refresh_leeway_seconds)
        return expiry - leeway <= now


def account_scopes(config: ServiceAccountConfig) -> tuple[str, ...]:
    return config.scopes or SERVICE_ACCOUNT_SCOPES


def _google_request() -> TokenRequest:
    """google-auth's transport, created lazily so tests never touch the network."""
    from google.auth.transport.requests import Request

    return Request()


def _error_detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:200]
    if isinstance(body, dict):
        for key in ("error_description", "error", "message"):
            value = body.get(key)
            if isinstance(value, str):
                return value[:200]
            if value is not None:
                return str(value)[:200]
    return str(body)[:200]
