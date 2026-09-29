"""Reading and organising the mailbox (PRD 9, Phase 4).

Reading needs a scope beyond `gmail.send`: `gmail.readonly` or `gmail.modify`. Organising needs
`gmail.modify` specifically, because labels are how read/unread, star, archive, and trash are
expressed. The check happens here, before the API call, so the agent is told the exact scope to add
instead of receiving an opaque 403 from Google.

Sending-only stays the default install. An operator grants mailbox access deliberately, and the
gateway keeps working without it: `read`, `list_messages`, and `modify_message` all refuse with
`scope_missing` on a send-only account rather than degrading silently.
"""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass
from datetime import datetime
from email import message_from_bytes
from email.header import decode_header, make_header
from email.message import Message
from typing import Any

from gmail_automator.clock import Clock, SystemClock
from gmail_automator.container import Container, require
from gmail_automator.errors import Forbidden, InvalidRequest, ScopeMissing
from gmail_automator.gmail.client import (
    GmailTransport,
    LabelInfo,
    LabelResult,
    MessageDetail,
    MessagePage,
    MessageSummary,
)
from gmail_automator.tokens import TokenManager

SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"

#: Either is enough to read. `gmail.modify` implies read; `gmail.readonly` grants nothing else.
READ_SCOPES: tuple[str, ...] = (
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.readonly",
)

#: Only `gmail.modify` writes labels. There is no narrower grant for it.
MODIFY_SCOPE = "https://www.googleapis.com/auth/gmail.modify"

#: The headers a fetch asks for. Deliberately the minimum a reply needs: the RFC Message-ID to put
#: in `In-Reply-To`, the existing `References` chain to extend, and the envelope fields an agent
#: reads to decide whether the message is worth opening.
REPLY_HEADERS: tuple[str, ...] = (
    "Message-ID",
    "In-Reply-To",
    "References",
    "Subject",
    "From",
    "To",
    "Cc",
    "Date",
)

#: Gmail system labels, spelled the way the API expects.
LABEL_UNREAD = "UNREAD"
LABEL_STARRED = "STARRED"
LABEL_INBOX = "INBOX"
LABEL_TRASH = "TRASH"
LABEL_SPAM = "SPAM"
LABEL_IMPORTANT = "IMPORTANT"

#: The friendly states an agent actually asks for, mapped to label operations. Anything not in here
#: is a raw label name, which is how a custom label gets set.
STATE_TO_LABELS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "read": ((), (LABEL_UNREAD,)),
    "unread": ((LABEL_UNREAD,), ()),
    "starred": ((LABEL_STARRED,), ()),
    "unstarred": ((), (LABEL_STARRED,)),
    "archived": ((), (LABEL_INBOX,)),
    "in_inbox": ((LABEL_INBOX,), ()),
    "trashed": ((LABEL_TRASH,), ()),
    "important": ((LABEL_IMPORTANT,), ()),
    "not_important": ((), (LABEL_IMPORTANT,)),
    "not_trashed": ((), (LABEL_TRASH,)),
    "not_spam": ((), (LABEL_SPAM,)),
}


def has_read_scope(account: Any) -> bool:
    granted = set(account.scopes or [])
    return any(scope in granted for scope in READ_SCOPES)


def has_modify_scope(account: Any) -> bool:
    return MODIFY_SCOPE in set(account.scopes or [])


def _require_scope(account: Any, scopes: tuple[str, ...], action: str) -> None:
    if any(scope in set(account.scopes or []) for scope in scopes):
        return
    granted = ", ".join(sorted(account.scopes or [])) or "none"
    raise ScopeMissing(
        f"{action} needs {' or '.join(scopes)}; {account.email} was connected with "
        f"{granted}. Reconnect it with a wider scope",
        details={
            "account": account.email,
            "required_scopes": list(scopes),
            "granted_scopes": account.scopes or [],
        },
    )


def decode_header_value(value: str | None) -> str:
    """Decode an RFC 2047 header into text.

    Subjects and display names arrive as `=?UTF-8?B?...?=`. Passing them through raw puts mojibake
    in a tool result, which an agent will then reason about as if it were real.
    """
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except (UnicodeDecodeError, LookupError, ValueError):
        return value


def unfold_header(value: str) -> str:
    """Collapse a folded header back onto one line.

    Python's email parser hands back the raw value, so a `References` chain that arrived folded
    still contains CRLF. Copying that into the reply's own headers would inject a line break into a
    header value, which is header injection rather than a cosmetic problem. Unfolding on the way in
    means the value is a plain single line everywhere downstream.
    """
    return re.sub(r"\r?\n[ \t]+", " ", value).replace("\r", " ").replace("\n", " ").strip()


def _header_map(message: Message) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in message.items():
        out[key.lower()] = unfold_header(decode_header_value(value))
    return out


def _charset_of(part: Message) -> str:
    """The declared charset, or utf-8. Mail in the wild is mostly utf-8 or ascii."""
    try:
        return part.get_content_charset() or "utf-8"
    except (LookupError, ValueError):
        return "utf-8"


def _walk_parts(message: Message) -> tuple[str | None, str | None]:
    """Find the best text and HTML bodies from a MIME tree.

    Prefers `text/plain` because an agent acting on mail should act on what the sender actually
    wrote, not on markup; falls back to HTML when a message has nothing else.
    `multipart/alternative` is walked in order so the first plain part wins.
    """
    plain: str | None = None
    html: str | None = None
    for part in message.walk():
        if part.get_content_maintype() == "multipart":
            continue
        disposition = (part.get("Content-Disposition") or "").lower()
        if "attachment" in disposition:
            continue
        content_type = part.get_content_type()
        if content_type not in ("text/plain", "text/html"):
            continue
        raw_part = part.get_payload(decode=True)
        encoded = raw_part if isinstance(raw_part, bytes) else b""
        body = encoded.decode(_charset_of(part), "replace")
        if content_type == "text/plain" and plain is None:
            plain = body
        elif content_type == "text/html" and html is None:
            html = body
    return plain, html


def _decode_payload_part(part: dict[str, Any]) -> str:
    data = part.get("body", {}).get("data")
    if not data:
        return ""
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", "replace")


def detail_from_payload(payload: dict[str, Any]) -> MessageDetail:
    """Turn a Gmail `messages.get` payload into a `MessageDetail`.

    Gmail hands the message as a MIME tree with base64url parts, and the threading headers in
    `payload.headers` as well as inside the parsed message. The parsed copy is preferred because it
    unfolds folded headers, which a `References` chain routinely is.
    """
    raw = _decode_payload_part(payload.get("payload") or {})
    headers: dict[str, str] = {}
    plain: str | None = None
    html: str | None = None
    if raw:
        parsed = message_from_bytes(raw.encode("utf-8", "replace"))
        headers = _header_map(parsed)
        plain, html = _walk_parts(parsed)
    # Gmail's own header list is the fallback when the MIME tree could not be parsed.
    for item in payload.get("payload", {}).get("headers", ()) or ():
        name = str(item.get("name", "")).lower()
        if name and name not in headers:
            headers[name] = decode_header_value(item.get("value"))
    envelope = payload.get("payload", {}) if "payload" in payload else {}
    return MessageDetail(
        id=str(payload.get("id", "")),
        thread_id=payload.get("threadId"),
        subject=headers.get("subject", ""),
        sender=headers.get("from", ""),
        recipients=headers.get("to", ""),
        date=headers.get("date", ""),
        snippet=str(envelope.get("snippet", "") or ""),
        label_ids=tuple(payload.get("labelIds") or ()),
        message_id_header=headers.get("message-id"),
        in_reply_to=headers.get("in-reply-to"),
        references=headers.get("references"),
        body_text=plain,
        body_html=html,
    )


@dataclass(frozen=True)
class MailboxService:
    """Search, read, and organise the mailbox behind explicit scope checks."""

    container: Container

    @property
    def clock(self) -> Clock:
        return self.container.clock or SystemClock()

    def _tokens(self) -> TokenManager:
        tokens: TokenManager = require(self.container, "tokens")
        return tokens

    def _transport(self) -> GmailTransport:
        transport: GmailTransport = require(self.container, "transport")
        return transport

    def _account(self, account_email: str | None) -> Any:
        account = require(self.container, "accounts").resolve(account_email)
        if account.status != "active":
            raise Forbidden(
                f"account {account.email} is {account.status}; reconnect it first",
                details={"account": account.email, "status": account.status},
            )
        return account

    def list_messages(
        self,
        *,
        account_email: str | None = None,
        query: str | None = None,
        max_results: int = 10,
        page_token: str | None = None,
        now: datetime | None = None,
    ) -> MessagePage:
        account = self._account(account_email)
        _require_scope(account, READ_SCOPES, "reading the mailbox")
        token = self._tokens().access_token(account.email, now=now)
        page: MessagePage = self._transport().list_messages(
            email=account.email,
            access_token=token.token,
            query=query,
            max_results=max_results,
            page_token=page_token,
        )
        return page

    def get_message(
        self,
        *,
        message_id: str,
        account_email: str | None = None,
        now: datetime | None = None,
    ) -> MessageDetail:
        account = self._account(account_email)
        _require_scope(account, READ_SCOPES, "reading a message")
        if not message_id:
            raise InvalidRequest("message_id is required")
        token = self._tokens().access_token(account.email, now=now)
        payload: dict[str, Any] = self._transport().get_message(
            email=account.email,
            access_token=token.token,
            message_id=message_id,
            headers=REPLY_HEADERS,
        )
        return detail_from_payload(payload)

    def list_labels(
        self,
        *,
        account_email: str | None = None,
        now: datetime | None = None,
    ) -> tuple[LabelInfo, ...]:
        account = self._account(account_email)
        _require_scope(account, (MODIFY_SCOPE,), "listing labels")
        token = self._tokens().access_token(account.email, now=now)
        labels: tuple[LabelInfo, ...] = self._transport().list_labels(
            email=account.email, access_token=token.token
        )
        return labels

    def modify_message(
        self,
        *,
        message_id: str,
        states: tuple[str, ...] = (),
        add_labels: tuple[str, ...] = (),
        remove_labels: tuple[str, ...] = (),
        account_email: str | None = None,
        now: datetime | None = None,
    ) -> LabelResult:
        """Set labels by friendly state names, or by raw label name.

        `states` exists so an agent asks for `read` rather than knowing that means "remove UNREAD",
        and `add_labels`/`remove_labels` remain for custom labels a person created in Gmail.
        """
        account = self._account(account_email)
        _require_scope(account, (MODIFY_SCOPE,), "modifying message labels")
        if not message_id:
            raise InvalidRequest("message_id is required")
        add: list[str] = []
        remove: list[str] = []
        for state in states:
            if state not in STATE_TO_LABELS:
                raise InvalidRequest(
                    f"unknown state {state!r}; use one of {', '.join(sorted(STATE_TO_LABELS))}, "
                    "or pass a raw label via add_labels/remove_labels"
                )
            state_add, state_remove = STATE_TO_LABELS[state]
            add.extend(state_add)
            remove.extend(state_remove)
        add.extend(add_labels)
        remove.extend(remove_labels)
        # A label in both lists is a contradiction, not a no-op; the add wins so the call is not
        # silently ignored.
        remove = [label for label in remove if label not in add]
        if not add and not remove:
            raise InvalidRequest(
                "nothing to change: pass at least one state, or add_labels/remove_labels"
            )
        token = self._tokens().access_token(account.email, now=now)
        result: LabelResult = self._transport().modify_message(
            email=account.email,
            access_token=token.token,
            message_id=message_id,
            add_label_ids=tuple(add),
            remove_label_ids=tuple(remove),
        )
        return result


__all__ = [
    "LABEL_IMPORTANT",
    "LABEL_INBOX",
    "LABEL_SPAM",
    "LABEL_STARRED",
    "LABEL_TRASH",
    "LABEL_UNREAD",
    "MODIFY_SCOPE",
    "READ_SCOPES",
    "REPLY_HEADERS",
    "SEND_SCOPE",
    "STATE_TO_LABELS",
    "LabelInfo",
    "MailboxService",
    "MessageDetail",
    "MessagePage",
    "MessageSummary",
    "decode_header_value",
    "detail_from_payload",
    "has_modify_scope",
    "has_read_scope",
]
