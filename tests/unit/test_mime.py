from datetime import UTC, datetime
from email import message_from_bytes
from email.policy import default as default_policy

import pytest

from fmaiily.errors import InvalidRequest
from fmaiily.gmail.mime import (
    Attachment,
    OutgoingMessage,
    build_mime,
    count_recipients,
    to_raw_b64,
)

NOW = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)


def _msg(**overrides) -> OutgoingMessage:
    base = {
        "from_email": "sender@example.com",
        "to": ("a@example.com",),
        "subject": "hello",
        "body": "plain body",
    }
    return OutgoingMessage(**{**base, **overrides})


def test_count_recipients_includes_cc_and_bcc() -> None:
    msg = _msg(to=("a@x.com", "b@x.com"), cc=("c@x.com",), bcc=("d@x.com", "e@x.com"))
    assert count_recipients(msg) == 5


def test_count_recipients_of_minimal_message() -> None:
    assert count_recipients(_msg()) == 1


def test_build_mime_sets_core_headers() -> None:
    raw = build_mime(_msg(), now=NOW)
    parsed = message_from_bytes(raw, policy=default_policy)
    assert parsed["From"] == "sender@example.com"
    assert parsed["To"] == "a@example.com"
    assert parsed["Subject"] == "hello"
    assert parsed["Date"] is not None
    assert parsed["Message-ID"].endswith("@example.com>")
    assert parsed["MIME-Version"] == "1.0"


def test_message_id_is_deterministic_for_same_input_and_time() -> None:
    first = build_mime(_msg(), now=NOW)
    second = build_mime(_msg(), now=NOW)
    assert first == second
    later = build_mime(_msg(), now=datetime(2026, 9, 26, 12, 0, 1, tzinfo=UTC))
    assert later != first


def test_message_id_domain_falls_back_when_sender_has_no_at() -> None:
    parsed = message_from_bytes(build_mime(_msg(from_email="noreply"), now=NOW))
    assert parsed["Message-ID"].endswith("@localhost>")


def test_plain_only_message_is_single_part() -> None:
    parsed = message_from_bytes(build_mime(_msg(), now=NOW), policy=default_policy)
    assert not parsed.is_multipart()
    assert parsed.get_content_type() == "text/plain"
    assert parsed.get_content().strip() == "plain body"


def test_html_alternative_produces_multipart_alternative() -> None:
    parsed = message_from_bytes(
        build_mime(_msg(body_html="<p>rich</p>"), now=NOW), policy=default_policy
    )
    assert parsed.is_multipart()
    types = [part.get_content_type() for part in parsed.iter_parts()]
    assert types == ["text/plain", "text/html"]
    html = parsed.get_body(preferencelist=("html",))
    assert html is not None and html.get_content().strip() == "<p>rich</p>"


def test_attachments_become_mixed_with_filenames() -> None:
    msg = _msg(attachments=(Attachment("a.txt", b"hello", "text/plain"),))
    parsed = message_from_bytes(build_mime(msg, now=NOW), policy=default_policy)
    assert parsed.is_multipart()
    assert parsed.get_content_type() == "multipart/mixed"
    parts = list(parsed.iter_parts())
    assert [p.get_content_type() for p in parts] == ["text/plain", "text/plain"]
    assert parts[1].get_filename() == "a.txt"
    assert parts[1].get_payload(decode=True) == b"hello"


def test_attachments_with_html_produce_mixed_wrapping_alternative() -> None:
    msg = _msg(body_html="<p>rich</p>", attachments=(Attachment("a.bin", b"\x00\x01"),))
    parsed = message_from_bytes(build_mime(msg, now=NOW), policy=default_policy)
    assert parsed.get_content_type() == "multipart/mixed"
    inner = next(iter(parsed.iter_parts()))
    assert inner.get_content_type() == "multipart/alternative"


def test_cc_and_reply_to_headers() -> None:
    msg = _msg(cc=("c@x.com",), reply_to="reply@example.com")
    parsed = message_from_bytes(build_mime(msg, now=NOW), policy=default_policy)
    assert parsed["Cc"] == "c@x.com"
    assert parsed["Reply-To"] == "reply@example.com"


def test_bcc_recipients_are_never_disclosed_in_headers() -> None:
    parsed = message_from_bytes(build_mime(_msg(bcc=("secret@x.com",)), now=NOW))
    assert "secret@x.com" not in parsed.as_string()
    assert "Bcc" not in parsed


def test_threading_headers() -> None:
    msg = _msg(in_reply_to="<parent@x>", references="<root@x> <parent@x>")
    parsed = message_from_bytes(build_mime(msg, now=NOW), policy=default_policy)
    assert parsed["In-Reply-To"] == "<parent@x>"
    assert parsed["References"] == "<root@x> <parent@x>"


def test_non_ascii_subject_is_encoded_per_rfc2047() -> None:
    raw = build_mime(_msg(subject="héllo wörld"), now=NOW)
    parsed = message_from_bytes(raw, policy=default_policy)
    assert parsed["Subject"] == "héllo wörld"
    assert b"=?utf-8?" in raw  # encoded form present on the wire


def test_output_uses_crlf_line_endings() -> None:
    raw = build_mime(_msg(), now=NOW)
    assert b"\r\n" in raw
    # every bare LF is illegal: each LF must be preceded by CR
    assert b"\n" not in raw.replace(b"\r\n", b"")


def test_header_injection_in_subject_is_rejected() -> None:
    with pytest.raises(InvalidRequest):
        build_mime(_msg(subject="hi\r\nBcc: attacker@evil.com"), now=NOW)


def test_header_injection_in_addresses_is_rejected() -> None:
    with pytest.raises(InvalidRequest):
        build_mime(_msg(to=("a@x.com\r\nBcc: attacker@evil.com",)), now=NOW)


def test_to_raw_b64_is_unpadded_urlsafe() -> None:
    encoded = to_raw_b64(b"\xfb\xff?")
    assert "=" not in encoded
    assert "+" not in encoded and "/" not in encoded


def test_to_raw_b64_roundtrip() -> None:
    import base64

    data = bytes(range(256))
    assert base64.urlsafe_b64decode(to_raw_b64(data) + "===") == data
