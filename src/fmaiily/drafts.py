"""Draft mode (PRD 9, Phase 3).

Creating a draft needs `gmail.compose` (or `gmail.modify`); an account connected with only
`gmail.send` cannot do it. The scope check happens here, before the API call, so the agent gets
`scope_missing` with the exact scope to add rather than an opaque 403 from Google.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fmaiily.clock import Clock, SystemClock
from fmaiily.container import Container, require
from fmaiily.errors import Forbidden, ScopeMissing
from fmaiily.gmail.client import DraftResult
from fmaiily.gmail.mime import OutgoingMessage, build_mime, to_raw_b64
from fmaiily.tokens import TokenManager

#: Either scope is sufficient; Gmail treats them as alternatives for draft creation.
COMPOSE_SCOPES: tuple[str, ...] = (
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.compose",
)

SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"


def has_compose_scope(account: Any) -> bool:
    granted = set(account.scopes or [])
    return any(scope in granted for scope in COMPOSE_SCOPES)


class DraftService:
    """Creates a Gmail draft instead of sending it.

    Drafts are a different product decision from sending: an operator usually wants to review the
    message in Gmail before it leaves. Nothing is queued - the draft lives in the mailbox, so
    there is no job, no quota reservation, and no history row.
    """

    def __init__(self, *, container: Container) -> None:
        self._container = container
        self._tokens: TokenManager = require(container, "tokens")
        self.clock: Clock = container.clock or SystemClock()

    def create_draft(
        self,
        *,
        account_email: str | None,
        msg: OutgoingMessage,
        now: datetime | None = None,
        thread_id: str | None = None,
    ) -> DraftResult:
        now = now or self.clock.now()
        accounts = require(self._container, "accounts")
        account = accounts.resolve(account_email)
        if account.status != "active":
            raise Forbidden(
                f"account {account.email} is {account.status}; reconnect it first",
                details={"account": account.email, "status": account.status},
            )
        if not has_compose_scope(account):
            raise ScopeMissing(
                f"creating a draft needs one of {', '.join(COMPOSE_SCOPES)}; "
                f"{account.email} was connected with only {SEND_SCOPE}. Reconnect it with "
                f"FMAIILY_OAUTH_SCOPES including the compose scope.",
                details={
                    "account": account.email,
                    "granted_scopes": list(account.scopes or []),
                    "required_scopes": list(COMPOSE_SCOPES),
                },
            )
        token = self._tokens.access_token(account.email, now=now)
        result: DraftResult = self._container.transport.create_draft(
            email=account.email,
            access_token=token.token,
            raw_b64url=to_raw_b64(build_mime(msg, now=now)),
            thread_id=thread_id,
        )
        return result


__all__ = ["COMPOSE_SCOPES", "SEND_SCOPE", "DraftService", "has_compose_scope"]
