from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass
from datetime import datetime
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import format_datetime

from fmaiily.errors import InvalidRequest

CRLF = "\r\n"


@dataclass(frozen=True)
class Attachment:
    filename: str
    content: bytes
    mime_type: str = "application/octet-stream"


@dataclass(frozen=True)
class OutgoingMessage:
    from_email: str
    to: tuple[str, ...]
    subject: str
    body: str  # plain text
    body_html: str | None = None
    cc: tuple[str, ...] = ()
    bcc: tuple[str, ...] = ()
    reply_to: str | None = None
    in_reply_to: str | None = None
    references: str | None = None
    attachments: tuple[Attachment, ...] = ()

    def recipient_count(self) -> int:
        return count_recipients(self)


def count_recipients(msg: OutgoingMessage) -> int:
    """Recipients Gmail counts against the daily cap: To + Cc + Bcc."""
    return len(msg.to) + len(msg.cc) + len(msg.bcc)


def _reject_header_injection(name: str, value: str) -> None:
    if "\r" in value or "\n" in value:
        raise InvalidRequest(f"{name} must not contain CR or LF characters")


def _message_id(msg: OutgoingMessage, *, now: datetime) -> str:
    """Deterministic Message-ID so the same message at the same instant is reproducible."""
    digest = hashlib.sha256(
        "\x00".join(
            [now.isoformat(), msg.from_email, msg.subject, msg.body, msg.body_html or ""]
        ).encode()
    ).hexdigest()[:32]
    domain = msg.from_email.split("@", 1)[1] if "@" in msg.from_email else ""
    return f"<{digest}@{domain or 'localhost'}>"


def build_mime(msg: OutgoingMessage, *, now: datetime) -> bytes:
    if now.tzinfo is None:
        raise InvalidRequest("`now` must be timezone-aware")

    _reject_header_injection("subject", msg.subject)
    _reject_header_injection("from_email", msg.from_email)
    for label, addresses in (
        ("to", msg.to),
        ("cc", msg.cc),
        ("bcc", msg.bcc),
    ):
        for address in addresses:
            _reject_header_injection(f"{label} address", address)
    for label, value in (
        ("reply_to", msg.reply_to),
        ("in_reply_to", msg.in_reply_to),
        ("references", msg.references),
    ):
        if value is not None:
            _reject_header_injection(label, value)

    root = EmailMessage(policy=SMTP)
    root["From"] = msg.from_email
    root["To"] = ", ".join(msg.to)
    root["Subject"] = msg.subject
    root["Date"] = format_datetime(now)
    root["Message-ID"] = _message_id(msg, now=now)
    if msg.cc:
        root["Cc"] = ", ".join(msg.cc)
    if msg.reply_to:
        root["Reply-To"] = msg.reply_to
    if msg.in_reply_to:
        root["In-Reply-To"] = msg.in_reply_to
    if msg.references:
        root["References"] = msg.references
    # Bcc recipients are deliberately absent from the transmitted headers.

    if msg.body_html is not None:
        root.set_content(msg.body)
        root.add_alternative(msg.body_html, subtype="html")
    else:
        root.set_content(msg.body)

    for attachment in msg.attachments:
        maintype, _, subtype = attachment.mime_type.partition("/")
        root.add_attachment(
            attachment.content,
            maintype=maintype or "application",
            subtype=subtype or "octet-stream",
            filename=attachment.filename,
        )

    return root.as_bytes()


def to_raw_b64(data: bytes) -> str:
    """Gmail `messages.send` expects the RFC 5322 payload as unpadded base64url."""
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


__all__ = [
    "Attachment",
    "OutgoingMessage",
    "build_mime",
    "count_recipients",
    "to_raw_b64",
]
