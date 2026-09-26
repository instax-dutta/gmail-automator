from __future__ import annotations

from dataclasses import dataclass

from fmaiily.errors import Forbidden, Unauthorized

#: Ordered from least to most privileged. A key's scope list is checked by exact membership.
SCOPE_SEND = "send"
SCOPE_READ = "read"
SCOPE_ADMIN = "admin"
ALL_SCOPES: tuple[str, ...] = (SCOPE_SEND, SCOPE_READ, SCOPE_ADMIN)


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


__all__ = [
    "ALL_SCOPES",
    "SCOPE_ADMIN",
    "SCOPE_READ",
    "SCOPE_SEND",
    "ApiKeyContext",
    "bearer_token_from_header",
]
