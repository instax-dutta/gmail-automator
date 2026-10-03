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
import binascii
import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
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
from gmail_automator.html_text import html_to_text
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


def _header_map(headers: Any) -> dict[str, str]:
    """Normalise a Gmail `headers` list into a lowercased name -> unfolded text map.

    Gmail hands headers over as `[{"name": ..., "value": ...}]` on every part of the tree, and a
    long header such as `References` arrives folded with the continuation indented. Unfolding on
    the way in means the value is a plain single line everywhere downstream, which is what keeps a
    reply from copying a CRLF into one of its own headers.
    """
    out: dict[str, str] = {}
    for item in headers or ():
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip().lower()
        if name and name not in out:
            out[name] = unfold_header(decode_header_value(item.get("value")))
    return out


def _declared_charset(content_type: str | None) -> str | None:
    """The charset a part's own `Content-Type` declares, if it is one we can name.

    Read from the part rather than the root because a part is the only thing that can be sure what
    its own bytes are: Exchange sends a Latin-1 body inside a `multipart/alternative` next to a
    utf-8 sibling, and the root header says nothing about either.
    """
    if not content_type:
        return None
    probe = Message()
    probe["Content-Type"] = content_type
    return probe.get_content_charset() or None


def _leaf_parts(part: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """Depth-first walk of Gmail's MIME tree, yielding the leaves in document order.

    A part with children holds no body of its own: for a multipart message Gmail leaves
    `payload.body.data` unset and puts every byte under `payload.parts`, so a walk that stops at
    the root finds nothing at all.

    Iterative rather than recursive on purpose. Nesting depth is chosen by the sender, and a
    message nested a couple of thousand levels deep is junk that must still be read as "no body"
    rather than raising `RecursionError` out of a tool call.
    """
    stack: list[dict[str, Any]] = [part]
    while stack:
        current = stack.pop()
        children = [child for child in (current.get("parts") or ()) if isinstance(child, dict)]
        if children:
            # Reversed, so popping yields the parts in the order the document lists them.
            stack.extend(reversed(children))
            continue
        yield current


def _mime_type_of(part: dict[str, Any], headers: dict[str, str]) -> str:
    """The part's MIME type, from the API field with its own header as the fallback."""
    declared = str(part.get("mimeType") or "").strip()
    if declared:
        return declared.split(";", 1)[0].strip().lower()
    return headers.get("content-type", "").split(";", 1)[0].strip().lower() or "text/plain"


def _decoded_part_bytes(part: dict[str, Any]) -> bytes | None:
    """A leaf's content, or None when it carries none.

    Gmail base64url-encodes `body.data` and strips the padding. The bytes are already transfer
    decoded, so nothing here applies a second `Content-Transfer-Encoding`: decoding them as text
    here and re-encoding later is what turns a Latin-1 body into U+FFFD, because the part's own
    charset cannot rescue bytes that were already destroyed.
    """
    data = part.get("body", {}).get("data") if isinstance(part.get("body"), dict) else None
    if not data:
        return None
    try:
        return base64.urlsafe_b64decode(str(data) + "=" * (-len(str(data)) % 4))
    except (binascii.Error, ValueError):
        return None


def _walk_parts(root: dict[str, Any]) -> tuple[str | None, str | None]:
    """Find the best text and HTML bodies from a Gmail payload tree.

    Prefers `text/plain` because an agent acting on mail should act on what the sender actually
    wrote, not on markup; falls back to HTML when a message has nothing else, which is the normal
    shape of an HTML-only message from Exchange. Attachment parts are skipped so an inline PDF's
    decoded bytes are never mistaken for the body.

    A part that decodes to nothing but whitespace counts as absent. Senders do ship a `text/plain`
    part that is empty precisely because they have nothing to say in it, and treating that as the
    body would leave the agent with an empty string where the HTML alternative had the message.
    """
    plain: str | None = None
    html: str | None = None
    for part in _leaf_parts(root):
        headers = _header_map(part.get("headers"))
        content_type = _mime_type_of(part, headers)
        if content_type not in ("text/plain", "text/html"):
            continue
        if "attachment" in headers.get("content-disposition", "").lower() or part.get("filename"):
            continue
        data = _decoded_part_bytes(part)
        if data is None:
            continue
        charset = _declared_charset(headers.get("content-type")) or "utf-8"
        try:
            body = data.decode(charset, "replace")
        except LookupError:
            body = data.decode("utf-8", "replace")
        if not body.strip():
            continue
        if content_type == "text/plain" and plain is None:
            plain = body
        elif content_type == "text/html" and html is None:
            html = body
    return plain, html


def _bodies_from_payload(root: dict[str, Any]) -> tuple[str | None, str | None]:
    """The plain and HTML bodies, deriving the plain one from the HTML when that is all there is.

    Exchange, Outlook and most notification senders offer only a `text/html` alternative, and
    `body_text` is the field an agent is told to read. Leaving it null for the majority of
    inbound business mail would make the preferred field the unreliable one, so the markup is
    rendered into prose. The markup is not discarded: `body_html` still carries it verbatim.
    """
    plain, html = _walk_parts(root)
    if plain is None and html is not None:
        plain = html_to_text(html)
    return plain, html


def detail_from_payload(payload: dict[str, Any]) -> MessageDetail:
    """Turn a Gmail `messages.get` payload into a `MessageDetail`.

    `format=full` returns a MIME tree rather than a message: the root part carries the headers,
    and each leaf carries its own content under `body.data`. Reading only the root `body.data`
    looks like it works, because for a single-part message that field holds the whole content -
    but it is unset on a multipart message, so every multipart message, HTML-only Outlook mail
    among them, came back with both bodies null. The tree is walked instead, and a message that
    offers only HTML gets its `body_text` rendered from that markup.
    """
    root = payload.get("payload") or {}
    headers = _header_map(root.get("headers"))
    plain, html = _bodies_from_payload(root)
    return MessageDetail(
        id=str(payload.get("id", "")),
        thread_id=payload.get("threadId"),
        subject=headers.get("subject", ""),
        sender=headers.get("from", ""),
        recipients=headers.get("to", ""),
        date=headers.get("date", ""),
        snippet=str(payload.get("snippet", "") or ""),
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
