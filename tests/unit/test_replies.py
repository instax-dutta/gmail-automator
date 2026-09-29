"""Reply threading: the part that is silently wrong if it is wrong.

A reply that carries the wrong `In-Reply-To` still sends, still arrives, and still looks fine in a
tool result. It just fails to thread. So these tests assert on the headers, not on "it sent".
"""

from __future__ import annotations

import base64

import pytest

from gmail_automator.gmail.mime import OutgoingMessage
from gmail_automator.mailbox import decode_header_value, detail_from_payload
from gmail_automator.replies import (
    extend_references,
    extract_address,
    reply_subject,
    reply_to,
)

BODY = "the original body"


def _payload(
    *,
    subject: str = "Original subject",
    sender: str = "Someone <someone@example.com>",
    to: str = "me@example.com",
    message_id: str = "<abc123@mail.example.com>",
    references: str | None = None,
    in_reply_to: str | None = None,
    body: str = BODY,
) -> dict:
    raw = (
        f"Message-ID: {message_id}\r\n"
        f"From: {sender}\r\n"
        f"To: {to}\r\n"
        f"Subject: {subject}\r\n"
        + (f"References: {references}\r\n" if references else "")
        + (f"In-Reply-To: {in_reply_to}\r\n" if in_reply_to else "")
        + f"Date: Tue, 29 Sep 2026 12:00:00 +0000\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n"
        "\r\n"
        f"{body}\r\n"
    ).encode()
    return {
        "id": "gmail-id-1",
        "threadId": "gmail-thread-1",
        "labelIds": ["INBOX"],
        "payload": {
            "headers": [],
            "body": {"data": base64.urlsafe_b64encode(raw).decode().rstrip("=")},
        },
    }


# ------------------------------------------------------------------ subject


@pytest.mark.parametrize(
    ("original", "expected"),
    [
        ("Original subject", "Re: Original subject"),
        ("Re: Original", "Re: Original"),
        ("RE: Shouting", "RE: Shouting"),
        ("re : already", "re : already"),
        ("Fwd: forwarded", "Fwd: forwarded"),
        ("FWD: forwarded", "FWD: forwarded"),
        ("", "Re:"),
        (None, "Re:"),
    ],
)
def test_reply_subject_never_doubles_the_prefix(original: str | None, expected: str) -> None:
    assert reply_subject(original) == expected


# --------------------------------------------------------------- references


def test_references_extends_the_existing_chain() -> None:
    chain = "<a@x> <b@y>"
    assert extend_references(chain, "<c@z>") == "<a@x> <b@y> <c@z>"


def test_references_starts_a_chain_when_there_was_none() -> None:
    assert extend_references(None, "<only@x>") == "<only@x>"


def test_references_does_not_duplicate_an_id_already_in_the_chain() -> None:
    """A re-sent header can repeat an id; a repeated References entry can loop some clients."""
    assert extend_references("<a@x> <b@y>", "<b@y>") == "<a@x> <b@y>"
    assert extend_references("<a@x> <a@x>", "<a@x>") == "<a@x>"


def test_references_passes_through_when_there_is_no_id_to_add() -> None:
    assert extend_references("<a@x>", None) == "<a@x>"
    assert extend_references(None, None) is None


# ------------------------------------------------------------------ address


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("Someone <someone@example.com>", "someone@example.com"),
        ("<someone@example.com>", "someone@example.com"),
        ("someone@example.com", "someone@example.com"),
        ("A B <a@b.co>, C D <c@d.co>", "a@b.co"),
        ("", ""),
        (None, ""),
    ],
)
def test_extract_address_unwraps_a_display_name(header: str | None, expected: str) -> None:
    assert extract_address(header) == expected


# ------------------------------------------------------------ payload parsing


def test_detail_from_payload_reads_the_body_and_threading_headers() -> None:
    detail = detail_from_payload(_payload(references="<a@x> <b@y>", in_reply_to="<b@y>"))
    assert detail.body_text is not None
    assert detail.body_text.strip() == BODY
    assert detail.message_id_header == "<abc123@mail.example.com>"
    assert detail.references == "<a@x> <b@y>"
    assert detail.in_reply_to == "<b@y>"
    assert detail.sender == "Someone <someone@example.com>"
    assert detail.thread_id == "gmail-thread-1"
    assert detail.label_ids == ("INBOX",)


def test_detail_prefers_the_parsed_mime_tree_over_the_envelope_headers() -> None:
    """Gmail lists headers twice; the parsed one is unfolded, which a References chain needs."""
    payload = _payload(references="<first@x>\r\n <second@x>")
    payload["payload"]["headers"] = [{"name": "Message-ID", "value": "<abc123@mail.example.com>"}]
    detail = detail_from_payload(payload)
    # A folded header unfolds to a single line; the raw envelope copy would keep the newline.
    assert "\n" not in detail.references
    assert detail.references.split() == ["<first@x>", "<second@x>"]


def test_detail_falls_back_to_envelope_headers_when_the_body_is_unparseable() -> None:
    payload = {
        "id": "x",
        "threadId": "t",
        "payload": {
            "body": {"data": ""},
            "headers": [
                {"name": "Message-ID", "value": "<fallback@x>"},
                {"name": "Subject", "value": "=?UTF-8?B?SGVsbG8=?="},
            ],
        },
    }
    detail = detail_from_payload(payload)
    assert detail.message_id_header == "<fallback@x>"
    assert detail.subject == "Hello"


def test_detail_survives_a_message_with_no_body() -> None:
    detail = detail_from_payload({"id": "x", "threadId": "t", "payload": {}})
    assert detail.body_text is None
    assert detail.body_html is None
    assert detail.id == "x"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("=?UTF-8?B?SGVsbG8=?=", "Hello"),
        ("plain subject", "plain subject"),
        ("=?UTF-8?Q?Caf=C3=A9?=", "Café"),
        ("", ""),
        (None, ""),
    ],
)
def test_decode_header_value_unpacks_rfc2047(raw: str | None, expected: str) -> None:
    assert decode_header_value(raw) == expected


# ------------------------------------------------------------------- building


def test_reply_to_builds_a_threaded_message_from_the_original() -> None:
    detail = detail_from_payload(_payload(references="<a@x> <b@y>"))
    message = reply_to(detail, account_email="me@example.com")
    assert isinstance(message, OutgoingMessage)
    assert message.to == ("someone@example.com",)
    assert message.subject == "Re: Original subject"
    assert message.in_reply_to == "<abc123@mail.example.com>"
    # The chain is extended, not replaced: the reply appends itself to what came before.
    assert message.references == "<a@x> <b@y> <abc123@mail.example.com>"
    assert message.from_email == "me@example.com"


def test_reply_to_refuses_when_there_is_nothing_to_reply_to() -> None:
    from gmail_automator.errors import InvalidRequest

    detail = detail_from_payload(_payload(sender=""))
    with pytest.raises(InvalidRequest):
        reply_to(detail, account_email="me@example.com")
