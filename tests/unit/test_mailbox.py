"""Turning a Gmail `messages.get` payload into a `MessageDetail`.

The bodies are the whole reason this file exists. `format=full` returns a MIME *tree*: the root
part carries the headers, a multipart message carries no body of its own, and every leaf keeps its
own content under its own `body.data`. Reading only the root body therefore looks correct on a
single-part message and returns nothing at all for a multipart one - so HTML-only mail, which
Exchange always sends as `multipart/alternative`, came back with both bodies null while the same
code path handled the gateway's own outbound plain-text messages fine.

Every payload here is built the way Gmail builds one, so a regression in the tree walk fails these
tests instead of reaching a mailbox.
"""

from __future__ import annotations

import base64
from email.message import EmailMessage

import pytest

from gmail_automator.mailbox import decode_header_value, detail_from_payload, unfold_header
from tests.support.gmail_payload import gmail_payload

# ------------------------------------------------------------------ builders


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _leaf(mime_type: str, data: bytes, *, extra_headers: dict[str, str] | None = None) -> dict:
    headers = {"Content-Type": mime_type, **(extra_headers or {})}
    return {
        "partId": "1",
        "mimeType": mime_type.split(";", 1)[0],
        "headers": [{"name": k, "value": v} for k, v in headers.items()],
        "body": {"size": len(data), "data": _b64(data)},
    }


def _text_part(mime_type: str, text: str, charset: str = "utf-8") -> dict:
    return _leaf(f"{mime_type}; charset={charset}", text.encode(charset))


def _outlook_html_only() -> bytes:
    """What Exchange sends for a mail with no plain alternative: HTML under multipart/alternative."""
    msg = EmailMessage()
    msg["Subject"] = "Your invoice is ready"
    msg["From"] = "Billing <billing@nebius.com>"
    msg["To"] = "ops@example.com"
    msg["Message-ID"] = "<abc123@nebius.com>"
    msg["Date"] = "Tue, 30 Sep 2025 10:00:00 +0000"
    msg.set_content("Your invoice is ready")
    msg.add_alternative(
        "<html><body><h1>Your invoice is ready</h1>"
        "<p>Invoice #5521 for 42.00 EUR is attached.</p></body></html>",
        subtype="html",
    )
    return msg.as_bytes()


# ------------------------------------------------------- the reported failure


def test_html_only_multipart_message_returns_its_html_body() -> None:
    """The bug: every multipart message parsed as if the root part held the body."""
    detail = detail_from_payload(gmail_payload(_outlook_html_only()))
    assert detail.body_html is not None
    assert "Invoice #5521 for 42.00 EUR is attached." in detail.body_html
    assert detail.subject == "Your invoice is ready"
    assert detail.sender == "Billing <billing@nebius.com>"
    assert detail.message_id_header == "<abc123@nebius.com>"


def test_a_single_part_html_message_reports_markup_and_prose_separately() -> None:
    """Markup belongs in `body_html`; `body_text` is the prose rendered out of it."""
    raw = (
        b"Subject: =?utf-8?B?SW52b2ljZQ==?=\r\n"
        b"From: billing@nebius.com\r\n"
        b"To: ops@example.com\r\n"
        b"Content-Type: text/html; charset=utf-8\r\n\r\n"
        b"<html><body><p>Invoice #7</p></body></html>\r\n"
    )
    detail = detail_from_payload(gmail_payload(raw))
    assert detail.body_text == "Invoice #7"
    assert "<p>" not in (detail.body_text or "")
    assert detail.body_html == "<html><body><p>Invoice #7</p></body></html>\r\n"
    assert detail.subject == "Invoice"


def test_plain_text_is_preferred_when_a_message_offers_both() -> None:
    detail = detail_from_payload(gmail_payload(_outlook_html_only()))
    assert detail.body_text == "Your invoice is ready\n"
    # The markup is still available, just not in preference to what the sender wrote.
    assert detail.body_html is not None and "<h1>" in detail.body_html


# ------------------------------------------------------------------- the walk


def test_a_nested_multipart_tree_is_walked_to_the_text_leaves() -> None:
    """multipart/mixed[multipart/alternative[text/plain, text/html], application/pdf]."""
    payload = {
        "id": "x",
        "threadId": "t",
        "payload": {
            "partId": "",
            "mimeType": "multipart/mixed",
            "headers": [{"name": "Subject", "value": "Invoice"}],
            "body": {"size": 0},
            "parts": [
                {
                    "partId": "1",
                    "mimeType": "multipart/alternative",
                    "headers": [{"name": "Content-Type", "value": "multipart/alternative"}],
                    "body": {"size": 0},
                    "parts": [
                        _text_part("text/plain", "the plain reading"),
                        _text_part("text/html", "<p>the markup</p>"),
                    ],
                },
                {
                    "partId": "2",
                    "mimeType": "application/pdf",
                    "headers": [{"name": "Content-Type", "value": "application/pdf"}],
                    "body": {"size": 4, "data": _b64(b"%PDF")},
                    "filename": "invoice.pdf",
                },
            ],
        },
    }
    detail = detail_from_payload(payload)
    assert detail.body_text == "the plain reading"
    assert detail.body_html == "<p>the markup</p>"


def test_an_attachment_part_is_not_mistaken_for_the_body() -> None:
    payload = {
        "id": "x",
        "threadId": "t",
        "payload": {
            "partId": "",
            "mimeType": "multipart/mixed",
            "headers": [{"name": "Subject", "value": "notes"}],
            "body": {"size": 0},
            "parts": [
                _text_part("text/plain", "see attached"),
                _leaf(
                    "text/plain; charset=utf-8",
                    b"this is a note.txt body, not the mail",
                    extra_headers={"Content-Disposition": 'attachment; filename="note.txt"'},
                ),
            ],
        },
    }
    detail = detail_from_payload(payload)
    assert detail.body_text == "see attached"


def test_an_empty_plain_part_falls_through_to_the_html_rather_than_winning() -> None:
    """Senders ship a `text/plain` part that is empty precisely because they have nothing to say."""
    payload = {
        "id": "x",
        "threadId": "t",
        "payload": {
            "partId": "",
            "mimeType": "multipart/alternative",
            "headers": [],
            "body": {"size": 0},
            "parts": [
                _leaf("text/plain; charset=utf-8", b""),
                _text_part("text/html", "<p>the only real body</p>"),
            ],
        },
    }
    detail = detail_from_payload(payload)
    assert detail.body_text == "the only real body"
    assert detail.body_html == "<p>the only real body</p>"


def test_mime_type_falls_back_to_the_part_header_when_the_field_is_absent() -> None:
    payload = {
        "id": "x",
        "threadId": "t",
        "payload": {
            "partId": "",
            "mimeType": "text/html",
            "headers": [],
            "body": {"data": _b64(b"<p>root only</p>")},
        },
    }
    detail = detail_from_payload(payload)
    assert detail.body_html == "<p>root only</p>"


def test_undecodable_part_data_yields_no_body_rather_than_an_exception() -> None:
    payload = {
        "id": "x",
        "threadId": "t",
        "payload": {
            "partId": "",
            "mimeType": "text/plain",
            "headers": [],
            "body": {"data": "!!!not base64!!!"},
        },
    }
    detail = detail_from_payload(payload)
    assert detail.body_text is None
    assert detail.body_html is None


def test_a_message_with_no_parts_has_no_bodies() -> None:
    detail = detail_from_payload({"id": "x", "threadId": "t", "payload": {}})
    assert detail.body_text is None
    assert detail.body_html is None
    assert detail.id == "x"


# --------------------------------------------------------------------- charsets


def test_a_part_declaring_latin1_is_decoded_as_latin1() -> None:
    """Exchange sends 8-bit Latin-1 freely; decoding it as utf-8 turned every accent into U+FFFD."""
    body = "Facture préparée pour 42,00 €"
    payload = {
        "id": "x",
        "threadId": "t",
        "payload": {
            "partId": "",
            "mimeType": "text/plain",
            "headers": [],
            "body": {"size": 0},
            "parts": [
                _leaf(
                    "text/plain; charset=iso-8859-1",
                    "Facture préparée\r\n".encode("iso-8859-1"),
                )
            ],
        },
    }
    detail = detail_from_payload(payload)
    assert detail.body_text == "Facture préparée\r\n"
    assert "�" not in (detail.body_text or "")
    assert body not in detail.body_text  # the euro sign is not in latin-1; the rest must survive


def test_a_charset_part_specific_to_one_sibling_does_not_leak_into_the_other() -> None:
    """A Latin-1 part and a utf-8 part in the same tree are decoded independently."""
    payload = {
        "id": "x",
        "threadId": "t",
        "payload": {
            "partId": "",
            "mimeType": "multipart/mixed",
            "headers": [{"name": "Content-Type", "value": "multipart/mixed"}],
            "body": {"size": 0},
            "parts": [
                _leaf("text/html; charset=iso-8859-1", "<p>café</p>".encode("iso-8859-1")),
                _text_part("text/plain", "plain — utf-8"),
            ],
        },
    }
    detail = detail_from_payload(payload)
    assert detail.body_html == "<p>café</p>"
    assert detail.body_text == "plain — utf-8"


def test_multibyte_utf8_survives_the_round_trip() -> None:
    detail = detail_from_payload(gmail_payload(_text_part_body()))
    assert detail.body_text is not None
    assert "café — naïve 你好" in detail.body_text


def _text_part_body() -> bytes:
    msg = EmailMessage()
    msg["Subject"] = "unicode"
    msg["From"] = "x@example.com"
    msg["To"] = "me@example.com"
    msg.set_content("café — naïve 你好")
    return msg.as_bytes()


def test_a_charset_nobody_recognises_falls_back_instead_of_raising() -> None:
    payload = {
        "id": "x",
        "threadId": "t",
        "payload": {
            "partId": "",
            "mimeType": "text/plain",
            "headers": [],
            "body": {"size": 0},
            "parts": [_leaf("text/plain; charset=x-not-a-charset", b"still readable")],
        },
    }
    detail = detail_from_payload(payload)
    assert detail.body_text == "still readable"


def test_an_unparseable_charset_parameter_falls_back_instead_of_raising() -> None:
    """`charset=` with nothing after it names no codec; the bytes must still be readable."""
    payload = {
        "id": "x",
        "threadId": "t",
        "payload": {
            "partId": "",
            "mimeType": "text/plain",
            "headers": [],
            "body": {"size": 0},
            "parts": [_leaf("text/plain; charset=", b"unlabelled bytes")],
        },
    }
    assert detail_from_payload(payload).body_text == "unlabelled bytes"


def test_a_junk_entry_in_the_parts_list_is_skipped() -> None:
    payload = {
        "id": "x",
        "threadId": "t",
        "payload": {
            "partId": "",
            "mimeType": "multipart/mixed",
            "headers": [],
            "body": {"size": 0},
            "parts": ["not-a-part", None, _text_part("text/plain", "the real body")],
        },
    }
    assert detail_from_payload(payload).body_text == "the real body"


# --------------------------------------------------------------------- headers


def test_a_whitespace_only_plain_part_falls_through_to_the_html() -> None:
    """Outlook's usual HTML-only shape is a `text/plain` part holding a blank line, not an empty one."""
    payload = {
        "id": "x",
        "threadId": "t",
        "payload": {
            "partId": "",
            "mimeType": "multipart/alternative",
            "headers": [],
            "body": {"size": 0},
            "parts": [
                _leaf("text/plain; charset=utf-8", b"\r\n \t\r\n"),
                _text_part("text/html", "<p>the message</p>"),
            ],
        },
    }
    detail = detail_from_payload(payload)
    assert detail.body_text == "the message"


def test_the_first_body_of_each_kind_wins() -> None:
    payload = {
        "id": "x",
        "threadId": "t",
        "payload": {
            "partId": "",
            "mimeType": "multipart/mixed",
            "headers": [],
            "body": {"size": 0},
            "parts": [
                _text_part("text/plain", "first plain"),
                _text_part("text/plain", "second plain"),
                _text_part("text/html", "<p>first html</p>"),
                _text_part("text/html", "<p>second html</p>"),
            ],
        },
    }
    detail = detail_from_payload(payload)
    assert detail.body_text == "first plain"
    assert detail.body_html == "<p>first html</p>"


def test_a_pathologically_nested_tree_does_not_recurse() -> None:
    """Nesting depth is the sender's choice, so junk that deep must read as "no body", not crash.

    A recursive walk raises `RecursionError` somewhere around two thousand levels, which would
    escape `detail_from_payload` and surface as an unhandled failure rather than a message.
    """
    node: dict = _text_part("text/html", "<p>deep</p>")
    for _ in range(20_000):
        node = {
            "partId": "p",
            "mimeType": "multipart/mixed",
            "headers": [],
            "body": {"size": 0},
            "parts": [node],
        }
    detail = detail_from_payload({"id": "x", "threadId": "t", "payload": node})
    assert detail.body_text == "deep"


def test_the_walk_visits_parts_in_document_order() -> None:
    """First of each kind wins, so the order has to be the document's and not the stack's."""
    payload = {
        "id": "x",
        "threadId": "t",
        "payload": {
            "partId": "",
            "mimeType": "multipart/mixed",
            "headers": [],
            "body": {"size": 0},
            "parts": [
                {
                    "partId": "0",
                    "mimeType": "multipart/alternative",
                    "headers": [],
                    "body": {"size": 0},
                    "parts": [_text_part("text/html", "<p>inner html</p>")],
                },
                _text_part("text/plain", "outer plain"),
            ],
        },
    }
    detail = detail_from_payload(payload)
    assert detail.body_html == "<p>inner html</p>"
    assert detail.body_text == "outer plain"


def test_headers_are_read_from_the_root_part_when_there_is_no_body() -> None:
    payload = {
        "id": "x",
        "threadId": "t",
        "payload": {
            "body": {"size": 0},
            "headers": [
                {"name": "Message-ID", "value": "<fallback@x>"},
                {"name": "Subject", "value": "=?UTF-8?B?SGVsbG8=?="},
            ],
        },
    }
    detail = detail_from_payload(payload)
    assert detail.message_id_header == "<fallback@x>"
    assert detail.subject == "Hello"


def test_a_folded_references_header_is_unfolded_from_gmails_own_copy() -> None:
    """Gmail hands the folded value over verbatim; copying it into a reply would inject a CRLF."""
    payload = gmail_payload(b"")
    payload["payload"]["headers"] = [{"name": "References", "value": "<first@x>\r\n <second@x>"}]
    detail = detail_from_payload(payload)
    assert detail.references is not None
    assert "\n" not in detail.references
    assert detail.references.split() == ["<first@x>", "<second@x>"]


def test_the_snippet_comes_from_the_message_not_the_payload_part() -> None:
    """`snippet` is a `Message` field. `MessagePart` has no such field, so reading it from `payload`
    returns nothing for every message, which is what an empty snippet on a real inbox means."""
    payload = gmail_payload(_outlook_html_only(), snippet="Invoice #5521 is ready")
    assert detail_from_payload(payload).snippet == "Invoice #5521 is ready"


def test_the_envelope_and_labels_are_carried_through_unchanged() -> None:
    payload = gmail_payload(_outlook_html_only(), label_ids=("INBOX", "UNREAD", "IMPORTANT"))
    detail = detail_from_payload(payload)
    assert detail.label_ids == ("INBOX", "UNREAD", "IMPORTANT")
    assert detail.thread_id == "thread-1"
    assert detail.snippet == "snippet"


def test_a_duplicate_header_name_keeps_the_first_value() -> None:
    payload = {
        "id": "x",
        "threadId": "t",
        "payload": {
            "headers": [
                {"name": "Subject", "value": "first"},
                {"name": "subject", "value": "second"},
            ]
        },
    }
    assert detail_from_payload(payload).subject == "first"


def test_a_malformed_header_entry_is_skipped() -> None:
    payload = {
        "id": "x",
        "threadId": "t",
        "payload": {
            "headers": ["not-a-dict", {"value": "no name"}, {"name": "Subject", "value": "ok"}]
        },
    }
    assert detail_from_payload(payload).subject == "ok"


# ------------------------------------------------------------------- the helpers


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


def test_decode_header_value_returns_the_input_when_it_cannot_be_decoded() -> None:
    undecodable = "=?utf-8?B?not-valid-base64!?="
    assert decode_header_value(undecodable) == undecodable


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("<a@x>\r\n <b@x>", "<a@x> <b@x>"),
        ("<a@x>\n\t<b@x>", "<a@x> <b@x>"),
        ("<a@x>", "<a@x>"),
    ],
)
def test_unfold_header_collapses_a_folded_value(value: str, expected: str) -> None:
    assert unfold_header(value) == expected
