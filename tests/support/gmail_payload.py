"""Build the JSON shape `users.messages.get?format=full` actually returns.

The doubles used to seed one flat payload with the entire RFC 822 message base64url-encoded into
the *root* `body.data`. Gmail never sends that: the root part holds only the headers, a multipart
message carries no root body at all, and every leaf keeps its own content in its own `body.data`.
A double that lies about the wire format hides exactly the bugs that matter in the parser, which
is how HTML-only mail shipped returning null bodies. These builders mirror the real shape so the
parsing path under test is the parsing path production uses.
"""

from __future__ import annotations

import base64
from email import message_from_bytes
from email.message import Message
from typing import Any

_PART_ID = [0]


def _next_part_id() -> str:
    _PART_ID[0] += 1
    return str(_PART_ID[0])


def _reset_part_ids() -> None:
    _PART_ID[0] = 0


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _content_bytes(part: Message) -> bytes:
    """A leaf's bytes, decoded from its Content-Transfer-Encoding like Gmail has already done."""
    decoded = part.get_payload(decode=True)
    if isinstance(decoded, bytes):
        return decoded
    raw = part.get_payload()
    return raw.encode("utf-8", "replace") if isinstance(raw, str) else b""


def gmail_part(part: Message) -> dict[str, Any]:
    """One node of Gmail's MIME tree, in the shape `MessagePart` documents."""
    mime_type = part.get_content_type()
    node: dict[str, Any] = {
        "partId": _next_part_id(),
        "mimeType": mime_type,
        "headers": [{"name": key, "value": value} for key, value in part.items()],
    }
    if part.is_multipart():
        node["body"] = {"size": 0}
        node["parts"] = [
            gmail_part(child) for child in part.get_payload() if isinstance(child, Message)
        ]
        return node
    data = _content_bytes(part)
    node["body"] = {"size": len(data), "data": _b64url(data)}
    if part.get_filename():
        node["filename"] = part.get_filename()
    return node


def gmail_payload(
    raw: bytes | str,
    *,
    message_id: str = "m1",
    thread_id: str | None = "thread-1",
    label_ids: tuple[str, ...] = ("INBOX", "UNREAD"),
    snippet: str = "snippet",
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    """A whole `messages.get` response for `raw`.

    `headers` overrides the root header list, for tests that want to hand the parser a header
    value the raw message cannot express, such as one folded across lines.
    """
    _reset_part_ids()
    source = raw.encode("utf-8") if isinstance(raw, str) else raw
    root = gmail_part(message_from_bytes(source))
    if headers is not None:
        root["headers"] = [{"name": key, "value": value} for key, value in headers.items()]
    return {
        "id": message_id,
        "threadId": thread_id,
        "labelIds": list(label_ids),
        # `snippet` is a field of `Message`, never of the `MessagePart` under `payload`. Putting it
        # inside the part is how a double ends up hiding a wrong read of it.
        "snippet": snippet,
        "payload": root,
    }


__all__ = ["gmail_part", "gmail_payload"]
