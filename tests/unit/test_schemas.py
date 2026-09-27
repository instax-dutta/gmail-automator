import pytest
from pydantic import ValidationError

from gmail_automator.gmail.mime import Attachment, OutgoingMessage
from gmail_automator.schemas import AttachmentIn, SendEmailRequest, to_outgoing_message


def test_minimal_send_request() -> None:
    req = SendEmailRequest(to=["a@example.com"], subject="hi", body="hello")
    assert req.wait is True and req.cc == [] and req.attachments == []


def test_to_is_required_and_nonempty() -> None:
    with pytest.raises(ValidationError):
        SendEmailRequest(to=[], subject="hi", body="x")


def test_invalid_email_rejected() -> None:
    with pytest.raises(ValidationError):
        SendEmailRequest(to=["not-an-email"], subject="hi", body="x")


def test_attachment_requires_exactly_one_source() -> None:
    with pytest.raises(ValidationError):
        AttachmentIn(filename="a.txt")
    with pytest.raises(ValidationError):
        AttachmentIn(filename="a.txt", content_base64="aGk=", path="/tmp/a.txt")
    assert AttachmentIn(filename="a.txt", content_base64="aGk=").content_base64 == "aGk="


def test_to_outgoing_message_maps_every_field() -> None:
    req = SendEmailRequest(
        to=["a@example.com", "b@example.com"],
        cc=["c@example.com"],
        bcc=["d@example.com"],
        subject="subj",
        body="plain",
        body_html="<p>rich</p>",
        reply_to="reply@example.com",
        in_reply_to="<parent@x>",
        references="<root@x> <parent@x>",
    )
    msg = to_outgoing_message(req, "me@example.com")
    assert isinstance(msg, OutgoingMessage)
    assert msg.from_email == "me@example.com"
    assert msg.to == ("a@example.com", "b@example.com")
    assert msg.cc == ("c@example.com",)
    assert msg.bcc == ("d@example.com",)
    assert msg.subject == "subj"
    assert msg.body == "plain"
    assert msg.body_html == "<p>rich</p>"
    assert msg.reply_to == "reply@example.com"
    assert msg.in_reply_to == "<parent@x>"
    assert msg.references == "<root@x> <parent@x>"
    assert msg.attachments == ()


def test_to_outgoing_message_keeps_lists_immutable() -> None:
    req = SendEmailRequest(to=["a@example.com"], subject="s", body="b")
    msg = to_outgoing_message(req, "me@example.com")
    assert isinstance(msg.to, tuple)


def test_attachment_mime_type_defaults_to_guessing() -> None:
    assert AttachmentIn(filename="a.txt", content_base64="aGk=").mime_type == ""


def test_to_outgoing_message_decodes_inline_attachments() -> None:
    req = SendEmailRequest(
        to=["a@example.com"],
        subject="s",
        body="b",
        attachments=[
            AttachmentIn(filename="a.txt", content_base64="aGVsbG8=", mime_type="text/plain")
        ],
    )
    msg = to_outgoing_message(req, "me@example.com")
    assert msg.attachments == (Attachment("a.txt", b"hello", "text/plain"),)


def test_to_outgoing_message_rejects_invalid_inline_base64() -> None:
    req = SendEmailRequest(
        to=["a@example.com"],
        subject="s",
        body="b",
        attachments=[AttachmentIn(filename="a.txt", content_base64="not base64!!")],
    )
    with pytest.raises(ValidationError):
        to_outgoing_message(req, "me@example.com")


def test_thread_id_is_carried_outside_the_message() -> None:
    req = SendEmailRequest(to=["a@example.com"], subject="s", body="b", thread_id="t-1")
    assert req.thread_id == "t-1"
    # thread_id is transport metadata, never a MIME header
    assert to_outgoing_message(req, "me@example.com").in_reply_to is None
