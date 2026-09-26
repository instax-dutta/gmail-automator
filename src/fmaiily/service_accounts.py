"""Service accounts and domain-wide delegation (Phase 3, P6).

A Workspace administrator can authorize a service account to impersonate a user. Fmaiily then mints
access tokens with a signed JWT assertion instead of an interactive consent flow, so an unattended
deployment can send as a Workspace mailbox - and there is no refresh token to store at all.

What the operator must do first is documented in `docs/google-cloud-setup.md`; the point of this
module is that the *runtime* side keeps the same guarantees: least-privilege scopes, tokens
encrypted at rest, and no silent scope widening.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlencode

from fmaiily.errors import InvalidRequest, SendFailed
from fmaiily.logging_setup import get_logger

_log = get_logger("fmaiily.service_account")

#: Least privilege, same as the interactive flow. Domain-wide delegation is scoped by the admin
#: grant, not by the scope string, so requesting more here would only widen the blast radius.
SERVICE_ACCOUNT_SCOPES: tuple[str, ...] = ("https://www.googleapis.com/auth/gmail.send",)

#: The JWT-bearer grant type, which is how a service account exchanges an assertion for a token.
JWT_BEARER_GRANT = "urn:ietf:params:oauth:grant-type:jwt-bearer"

#: Google caps a service-account access token at one hour.
DEFAULT_TOKEN_LIFETIME_SECONDS = 3600

REQUIRED_FIELDS = (
    "type",
    "client_email",
    "private_key",
    "token_uri",
)


class TokenRequest(Protocol):
    """The subset of `google.auth.transport.Request` a credential refresh needs.

    Kept as a protocol so the whole flow is testable without a network or a real transport.
    """

    def __call__(
        self,
        url: str,
        method: str = "GET",
        body: str | bytes | None = None,
        headers: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> tuple[int, dict[str, str], bytes]: ...


@dataclass(frozen=True)
class ServiceAccountConfig:
    client_email: str
    private_key: str
    token_uri: str
    subject: str
    project_id: str | None = None
    private_key_id: str | None = None
    client_id: str | None = None
    scopes: tuple[str, ...] = field(default=SERVICE_ACCOUNT_SCOPES)

    @classmethod
    def from_key(
        cls, payload: dict[str, Any], *, subject: str, scopes: tuple[str, ...] | None = None
    ) -> ServiceAccountConfig:
        return parse_service_account_json(
            json.dumps(payload),
            subject=subject,
            scopes=scopes or SERVICE_ACCOUNT_SCOPES,
        )

    def redacted(self) -> dict[str, Any]:
        """Everything except the private key, for logs and CLI output."""
        return {
            "client_email": self.client_email,
            "subject": self.subject,
            "project_id": self.project_id,
            "token_uri": self.token_uri,
            "scopes": list(self.scopes),
        }


def parse_service_account_json(
    raw: str | dict[str, Any],
    *,
    subject: str,
    scopes: tuple[str, ...] | list[str] | None = None,
) -> ServiceAccountConfig:
    if not subject.strip():
        raise InvalidRequest(
            "a service account needs a subject: the Workspace user to impersonate "
            "(FMAIILY_SERVICE_ACCOUNT_SUBJECT). Without it there is nothing to send as and no "
            "domain-wide delegation to rely on.",
            details={"field": "subject"},
        )
    try:
        payload = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError as exc:
        raise InvalidRequest(
            "the service account key is not valid JSON", details={"field": "json"}
        ) from exc
    if not isinstance(payload, dict):
        raise InvalidRequest(
            "the service account key must be a JSON object", details={"field": "json"}
        )
    if payload.get("type") != "service_account":
        raise InvalidRequest(
            "the supplied key is not a service account key (type must be 'service_account')",
            details={"field": "type", "found": payload.get("type")},
        )
    missing = [name for name in REQUIRED_FIELDS if not payload.get(name)]
    if missing:
        raise InvalidRequest(
            f"the service account key is missing: {', '.join(missing)}",
            details={"missing": missing},
        )
    return ServiceAccountConfig(
        client_email=str(payload["client_email"]),
        private_key=str(payload["private_key"]),
        token_uri=str(payload["token_uri"]),
        subject=subject.strip(),
        project_id=payload.get("project_id"),
        private_key_id=payload.get("private_key_id"),
        client_id=payload.get("client_id"),
        scopes=tuple(scopes or SERVICE_ACCOUNT_SCOPES),
    )


def load_service_account_key(path: Path | str, *, subject: str) -> ServiceAccountConfig:
    target = Path(path).expanduser()
    try:
        raw = target.read_text(encoding="utf-8")
    except OSError as exc:
        raise InvalidRequest(
            f"the service account key could not be read from {target}",
            details={"path": str(target)},
        ) from exc
    return parse_service_account_json(raw, subject=subject)


def build_assertion(
    config: ServiceAccountConfig,
    *,
    now: datetime,
    scopes: tuple[str, ...] | list[str] | None = None,
) -> str:
    """Build the signed JWT assertion that a domain-wide-delegated token request needs.

    Built here rather than delegated to `google.oauth2.service_account` for two reasons: the
    `iat`/`exp` claims come from the injected `now` so they are deterministic, and the audience is
    the configured `token_uri` rather than a hard-coded Google constant. Signing uses PyJWT, which
    google-auth already depends on.
    """
    import jwt

    wanted = tuple(scopes or config.scopes)
    issued_at = int(now.timestamp())
    payload = {
        "iss": config.client_email,
        "sub": config.subject,
        "aud": config.token_uri,
        "scope": " ".join(wanted),
        "iat": issued_at,
        "exp": issued_at + DEFAULT_TOKEN_LIFETIME_SECONDS,
    }
    return jwt.encode(payload, config.private_key, algorithm="RS256")


def mint_access_token(
    config: ServiceAccountConfig,
    *,
    request: TokenRequest,
    now: datetime,
    scopes: tuple[str, ...] | list[str] | None = None,
) -> tuple[str, datetime]:
    """Exchange a signed assertion for an access token.

    Returns the token and its expiry. Nothing is persisted: a service account can always mint a new
    one, which is why no refresh token is involved anywhere in this path.
    """
    wanted = tuple(scopes or config.scopes)
    assertion = build_assertion(config, now=now, scopes=wanted)
    body = urlencode(
        {
            "grant_type": JWT_BEARER_GRANT,
            "assertion": assertion,
        }
    )
    try:
        status, _headers, payload = request(
            config.token_uri,
            method="POST",
            body=body,
            headers={
                "content-type": "application/x-www-form-urlencoded",
                "user-agent": "fmaiily/1.0",
            },
        )
    except Exception as exc:  # any transport failure is an upstream problem
        raise SendFailed(
            f"the service account assertion could not be sent: {exc}",
            details={"client_email": config.client_email, "subject": config.subject},
        ) from exc

    if status >= 400:
        detail = _error_detail(payload)
        _log.warning(
            "service_account_assertion_rejected",
            client_email=config.client_email,
            subject=config.subject,
            status=status,
        )
        raise SendFailed(
            f"the service account assertion was rejected (HTTP {status}): {detail}",
            details={"client_email": config.client_email, "subject": config.subject},
        )

    try:
        parsed = json.loads(payload.decode() if isinstance(payload, bytes) else payload)
    except ValueError as exc:
        raise SendFailed("the token endpoint returned a non-JSON response") from exc
    token = parsed.get("access_token")
    if not token:
        raise SendFailed(
            "the token endpoint returned no access token",
            details={"client_email": config.client_email},
        )
    expires_in = int(parsed.get("expires_in") or DEFAULT_TOKEN_LIFETIME_SECONDS)
    return str(token), now + timedelta(seconds=expires_in)


def _error_detail(payload: bytes | str) -> str:
    text = payload.decode() if isinstance(payload, bytes) else payload
    try:
        parsed = json.loads(text)
    except ValueError:
        return text[:200]
    if isinstance(parsed, dict):
        for key in ("error_description", "error", "message"):
            value = parsed.get(key)
            if isinstance(value, str):
                return value[:200]
    return str(parsed)[:200]


__all__ = [
    "DEFAULT_TOKEN_LIFETIME_SECONDS",
    "JWT_BEARER_GRANT",
    "SERVICE_ACCOUNT_SCOPES",
    "ServiceAccountConfig",
    "TokenRequest",
    "build_assertion",
    "load_service_account_key",
    "mint_access_token",
    "parse_service_account_json",
]
