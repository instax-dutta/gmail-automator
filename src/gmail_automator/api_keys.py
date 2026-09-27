from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from gmail_automator.errors import Forbidden, InvalidRequest, Unauthorized

if TYPE_CHECKING:
    from gmail_automator.container import Container

#: Ordered from least to most privileged. A key's scope list is checked by exact membership.
SCOPE_SEND = "send"
SCOPE_READ = "read"
SCOPE_ADMIN = "admin"
ALL_SCOPES: tuple[str, ...] = (SCOPE_SEND, SCOPE_READ, SCOPE_ADMIN)

#: R8 wire format: `fmg_<8 char prefix>_<43 char urlsafe secret>`. The secret carries 256 bits of
#: entropy, so a single SHA-256 pass is the right hash; Argon2 exists to slow down guessing of
#: low-entropy passwords, which is not this threat model.
KEY_PREFIX = "fmg_"
PREFIX_LEN = 8
SECRET_LEN = 43
KEY_SEPARATOR = "_"


@dataclass(frozen=True)
class ApiKeyContext:
    """Who is making the request, for permission checks and per-key accounting.

    Phase 1 only needs the identity: full key issuance, rotation, and per-key rate limits land in
    Phase 2. `key_id` is None for the local/bootstrap identity, which has no database row.
    """

    key_id: int | None
    name: str
    scopes: tuple[str, ...] = ALL_SCOPES
    allowed_accounts: tuple[str, ...] | None = None

    def has_scope(self, scope: str) -> bool:
        return scope in self.scopes

    def require_scope(self, scope: str) -> None:
        if not self.has_scope(scope):
            raise Forbidden(
                f"this API key lacks the '{scope}' scope",
                details={"required_scope": scope, "key": self.name},
            )

    def allows_account(self, email: str) -> bool:
        if self.allowed_accounts is None:
            return True
        return email.strip().lower() in {a.strip().lower() for a in self.allowed_accounts}

    def require_account(self, email: str) -> None:
        if not self.allows_account(email):
            raise Forbidden(
                "this API key is not permitted to use that account",
                details={"account": email, "key": self.name},
            )

    @classmethod
    def local(cls) -> ApiKeyContext:
        return cls(key_id=None, name="local")


def bearer_token_from_header(authorization: str | None) -> str:
    if not authorization:
        raise Unauthorized("missing Authorization header")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise Unauthorized("expected an `Authorization: Bearer <key>` header")
    return token.strip()


def generate_key() -> tuple[str, str, str]:
    """Return `(full_key, key_prefix, key_hash)`; the full key is never stored."""
    prefix = KEY_PREFIX + secrets.token_hex(PREFIX_LEN // 2)
    secret = secrets.token_urlsafe(48)[:SECRET_LEN]
    full = f"{prefix}{KEY_SEPARATOR}{secret}"
    return full, prefix, hash_key(secret)


def hash_key(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def split_key(full_key: str) -> tuple[str, str] | None:
    """Split `fmg_<prefix>_<secret>` into `(key_prefix, secret)`.

    The prefix itself contains the `fmg_` scheme marker, so the key has two separators, not one.
    """
    parts = full_key.strip().split("_", 2)
    if len(parts) != 3 or parts[0] != KEY_PREFIX.rstrip("_"):
        return None
    scheme, prefix, secret = parts
    if not prefix or not secret:
        return None
    return f"{scheme}_{prefix}", secret


class ApiKeyService:
    """Issuance and verification of gateway API keys (master plan R8)."""

    def __init__(self, *, container: Container) -> None:
        self._container = container

    def create(
        self,
        *,
        name: str,
        scopes: tuple[str, ...] = (SCOPE_SEND, SCOPE_READ),
        allowed_accounts: tuple[str, ...] | None = None,
        rate_limit_per_minute: int | None = None,
    ) -> tuple[ApiKeyContext, str]:
        from gmail_automator.models import ApiKeyRow

        if not name.strip():
            raise InvalidRequest("an API key needs a name")
        unknown = set(scopes) - set(ALL_SCOPES)
        if unknown:
            raise InvalidRequest(
                f"unknown scope(s): {sorted(unknown)}", details={"allowed": list(ALL_SCOPES)}
            )
        full_key, prefix, key_hash = generate_key()
        now = self._container.clock.now()
        with self._container.session_factory() as session:
            row = ApiKeyRow(
                name=name.strip()[:80],
                key_prefix=prefix,
                key_hash=key_hash,
                scopes=list(scopes),
                allowed_accounts=list(allowed_accounts) if allowed_accounts else None,
                rate_limit_per_minute=rate_limit_per_minute,
                created_at=now,
            )
            session.add(row)
            session.commit()
            key_id = row.id
        assert key_id is not None
        return (
            ApiKeyContext(
                key_id=key_id,
                name=name.strip()[:80],
                scopes=tuple(scopes),
                allowed_accounts=allowed_accounts,
            ),
            full_key,
        )

    def verify(self, full_key: str) -> ApiKeyContext:
        from gmail_automator.models import ApiKeyRow

        parts = split_key(full_key.strip())
        if parts is None:
            raise Unauthorized("malformed API key")
        prefix, secret = parts
        with self._container.session_factory() as session:
            row = session.scalar(select(ApiKeyRow).where(ApiKeyRow.key_prefix == prefix))
            if row is None or not hmac.compare_digest(row.key_hash, hash_key(secret)):
                raise Unauthorized("unknown or invalid API key")
            if row.revoked_at is not None:
                raise Unauthorized("this API key has been revoked")
            row.last_used_at = self._container.clock.now()
            session.commit()
            return ApiKeyContext(
                key_id=row.id,
                name=row.name,
                scopes=tuple(row.scopes or ()),
                allowed_accounts=tuple(row.allowed_accounts) if row.allowed_accounts else None,
            )

    def list_keys(self) -> list[dict[str, Any]]:
        from gmail_automator.models import ApiKeyRow

        with self._container.session_factory() as session:
            rows = list(session.scalars(select(ApiKeyRow).order_by(ApiKeyRow.id)))
            return [
                {
                    "id": row.id,
                    "name": row.name,
                    "key_prefix": row.key_prefix,
                    "scopes": list(row.scopes or []),
                    "allowed_accounts": list(row.allowed_accounts)
                    if row.allowed_accounts
                    else None,
                    "rate_limit_per_minute": row.rate_limit_per_minute,
                    "created_at": row.created_at,
                    "last_used_at": row.last_used_at,
                    "revoked_at": row.revoked_at,
                }
                for row in rows
            ]

    def revoke(self, prefix: str) -> None:
        from gmail_automator.models import ApiKeyRow

        with self._container.session_factory() as session:
            row = session.scalar(select(ApiKeyRow).where(ApiKeyRow.key_prefix == prefix))
            if row is None:
                raise InvalidRequest("no such API key prefix", details={"prefix": prefix})
            row.revoked_at = self._container.clock.now()
            session.commit()


__all__ = [
    "ALL_SCOPES",
    "KEY_PREFIX",
    "SCOPE_ADMIN",
    "SCOPE_READ",
    "SCOPE_SEND",
    "ApiKeyContext",
    "ApiKeyService",
    "bearer_token_from_header",
    "generate_key",
    "hash_key",
    "split_key",
]
