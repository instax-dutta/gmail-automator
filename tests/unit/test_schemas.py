import pytest
from pydantic import ValidationError

from fmaiily.schemas import AttachmentIn, SendEmailRequest


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
