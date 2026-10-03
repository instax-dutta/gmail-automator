"""Reply threading: the part that is silently wrong if it is wrong.

A reply that carries the wrong `In-Reply-To` still sends, still arrives, and still looks fine in a
tool result. It just fails to thread. So these tests assert on the headers, not on "it sent".
"""

from __future__ import annotations

import pytest

from gmail_automator.gmail.mime import OutgoingMessage
from gmail_automator.mailbox import detail_from_payload
from gmail_automator.replies import (
    extend_references,
    extract_address,
    reply_subject,
    reply_to,
)
from tests.support.gmail_payload import gmail_payload

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
    return gmail_payload(
        raw,
        message_id="gmail-id-1",
        thread_id="gmail-thread-1",
        label_ids=("INBOX",),
        snippet="",
    )


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
