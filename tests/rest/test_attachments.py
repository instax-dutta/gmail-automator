"""Attachments through the HTTP surface (Phase 3, P2).

`resolve_attachments` is unit tested in isolation; these tests prove the wiring: a request with a
`path` attachment is confined by the allow-list at the edge, not deep inside the send path.
"""

import base64
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from fmaiily.rest.app import create_app
from fmaiily.worker import Worker


@pytest.fixture
def allowed(tmp_path: Path) -> Path:
    directory = tmp_path / "files"
    directory.mkdir()
    (directory / "report.txt").write_text("quarterly numbers")
    (directory / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n" + bytes(64))
    return directory


@pytest.fixture
def attach_client(http_container, allowed: Path):
    """The same app, with attachments enabled and one allow-listed directory."""
    container = http_container
    container.settings = container.settings.model_copy(
        update={
            "attachments_enabled": True,
            "attachment_allowed_dirs": [str(allowed)],
        }
    )
    # services hold the settings they were built with, so rebuild the sender with the new policy
    from fmaiily.send import SendService

    container.sender = SendService(
        settings=container.settings,
        accounts=container.accounts,
        quota=container.quota,
        queue=container.queue,
        clock=container.clock,
        sleeper=container.sleeper,
    )
    app = create_app(container, settings=container.settings, start_worker=False)
    with TestClient(app) as client:
        yield client


def _body(**attachment) -> dict:
    return {
        "to": ["a@example.com"],
        "subject": "with attachment",
        "body": "see attached",
        "wait": False,
        "attachments": [attachment],
    }


def test_an_allowed_path_is_queued(attach_client: TestClient, allowed: Path, connected) -> None:
    response = attach_client.post(
        "/v1/send", json=_body(filename="report.txt", path=str(allowed / "report.txt"))
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "queued"


def test_an_outside_path_is_refused(attach_client: TestClient, tmp_path: Path, connected) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_text("do not read me")
    response = attach_client.post("/v1/send", json=_body(filename="secret.txt", path=str(secret)))
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "attachment_path_not_allowed"
    assert "FMAIILY_ATTACHMENT_ALLOWED_DIRS" in error["message"]


def test_inline_content_still_works(attach_client: TestClient, connected: str) -> None:
    response = attach_client.post(
        "/v1/send",
        json=_body(
            filename="inline.txt",
            content_base64=base64.b64encode(b"inline body").decode(),
            mime_type="text/plain",
        ),
    )
    assert response.status_code == 200, response.text


def test_the_attachment_reaches_gmail_intact(http_container, allowed: Path, fake_transport) -> None:
    """Send -> queue -> worker -> transport, asserting the decoded MIME carries the file."""
    from fmaiily.gmail.mime import OutgoingMessage
    from fmaiily.schemas import SendEmailRequest, to_outgoing_message

    http_container.settings = http_container.settings.model_copy(
        update={
            "attachments_enabled": True,
            "attachment_allowed_dirs": [str(allowed)],
        }
    )
    from fmaiily.send import SendService

    http_container.sender = SendService(
        settings=http_container.settings,
        accounts=http_container.accounts,
        quota=http_container.quota,
        queue=http_container.queue,
        clock=http_container.clock,
        sleeper=http_container.sleeper,
    )
    start = http_container.oauth.start()
    account = http_container.oauth.callback(code="c", state=start.state).email

    request = SendEmailRequest.model_validate(
        {
            "to": ["a@example.com"],
            "subject": "with attachment",
            "body": "see attached",
            "wait": False,
            "attachments": [{"filename": "report.txt", "path": str(allowed / "report.txt")}],
        }
    )
    message: OutgoingMessage = to_outgoing_message(
        request, account, settings=http_container.settings
    )
    assert message.attachments[0].content == b"quarterly numbers"
    assert message.attachments[0].mime_type == "text/plain"

    http_container.sender.send(account_email=account, msg=message, source="api", wait=False)
    worker = Worker(http_container, worker_id="w", rand=lambda: 0.0)
    assert worker.run_once().action == "sent"

    import base64 as b64
    from email import message_from_bytes
    from email.policy import default as policy

    raw = b64.urlsafe_b64decode(fake_transport.calls[0]["raw_b64url"] + "===")
    parsed = message_from_bytes(raw, policy=policy)
    parts = {part.get_filename(): part for part in parsed.iter_parts() if part.get_filename()}
    assert b"quarterly numbers" in parts["report.txt"].get_payload(decode=True)


def test_a_batch_refuses_the_whole_batch_when_one_attachment_is_bad(
    attach_client: TestClient, tmp_path: Path, connected
) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_text("do not read me")
    response = attach_client.post(
        "/v1/send/batch",
        json={
            "emails": [
                {"to": ["a@example.com"], "subject": "ok", "body": "b", "wait": False},
                {
                    "to": ["b@example.com"],
                    "subject": "bad",
                    "body": "b",
                    "wait": False,
                    "attachments": [{"filename": "secret.txt", "path": str(secret)}],
                },
            ],
            "wait": False,
        },
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "attachment_path_not_allowed"
    assert attach_client.get("/v1/history").json()["items"] == []
