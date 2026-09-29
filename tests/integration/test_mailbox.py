"""Mailbox read/organise and reply, against a real database and the fake transport.

These assert the wiring an agent actually depends on: the scope gate refuses before any API call,
the reply carries the original's threading headers, and the same rules apply whether a capability
is reached over MCP or REST.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from gmail_automator.container import build_container
from gmail_automator.errors import InvalidRequest, ScopeMissing
from gmail_automator.mailbox import LABEL_INBOX, LABEL_STARRED, LABEL_UNREAD
from gmail_automator.worker import Worker
from tests.support.fakes import FakeClock, FakeGmailTransport

NOW = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
SEND_ONLY = ["https://www.googleapis.com/auth/gmail.send"]
FULL = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.modify",
]


def _decode(raw_b64url: str) -> dict[str, str]:
    """Decode a sent message back into headers, the way a mail client would read it.

    Kept here rather than added to the package: nothing in production needs to parse its own output,
    and a test-only helper in `mime.py` would be public API forever.
    """
    import base64
    from email import message_from_bytes

    padded = raw_b64url + "=" * (-len(raw_b64url) % 4)
    parsed = message_from_bytes(base64.urlsafe_b64decode(padded))
    return {key: parsed[key] or "" for key in parsed}


def _build(settings, engine, transport, sleeper, *, scopes: list[str]):
    container = build_container(
        settings,
        engine=engine,
        transport=transport,
        clock=FakeClock(),
        sleeper=sleeper,
    )
    email = "me@example.com"
    # A connected OAuth account holding exactly the scopes under test, with real encrypted tokens
    # so `access_token()` succeeds and the scope check is the only thing under test.
    from datetime import timedelta

    cipher = container.cipher
    container.accounts.upsert_oauth_account(
        email=email,
        account_type="personal",
        access_token_enc=cipher.encrypt("cached-access-token", aad=email),
        refresh_token_enc=cipher.encrypt("1//stored-refresh", aad=email),
        expiry=NOW + timedelta(hours=1),
        scopes=list(scopes),
        token_uri="http://oauth.test/token",
        now=NOW,
    )
    return container, email


@pytest.fixture
def send_only(settings, seeded_engine, sleeper):
    transport = FakeGmailTransport()
    container, email = _build(settings, seeded_engine, transport, sleeper, scopes=SEND_ONLY)
    return container, transport, email


@pytest.fixture
def full_access(settings, seeded_engine, sleeper):
    transport = FakeGmailTransport()
    container, email = _build(settings, seeded_engine, transport, sleeper, scopes=FULL)
    return container, transport, email


# ------------------------------------------------------------- scope refusal


def test_reading_is_refused_on_a_send_only_account(send_only) -> None:
    container, transport, email = send_only
    with pytest.raises(ScopeMissing) as excinfo:
        container.mailbox.list_messages(account_email=email, now=NOW)
    assert "gmail.modify" in str(excinfo.value) or "gmail.readonly" in str(excinfo.value)
    # The refusal happened before any API call, which is the point of checking here.
    assert transport.calls == []


def test_modifying_labels_is_refused_without_the_modify_scope(send_only) -> None:
    container, transport, email = send_only
    with pytest.raises(ScopeMissing):
        container.mailbox.modify_message(
            message_id="m1", states=("read",), account_email=email, now=NOW
        )
    assert transport.calls == []


def test_listing_labels_is_refused_without_the_modify_scope(send_only) -> None:
    container, _transport, email = send_only
    with pytest.raises(ScopeMissing):
        container.mailbox.list_labels(account_email=email, now=NOW)


# ------------------------------------------------------------------- reading


def test_list_messages_returns_summaries_and_keeps_the_query_intact(full_access) -> None:
    container, transport, email = full_access
    transport.seed_message("m1", subject="Hello", snippet="a snippet")
    page = container.mailbox.list_messages(
        account_email=email, query="is:unread", max_results=5, now=NOW
    )
    assert [m.id for m in page.messages] == ["m1"]
    assert page.messages[0].snippet == "a snippet"
    # Gmail's own syntax is passed through, not reimplemented.
    listed = [c for c in transport.calls if c.get("op") == "list_messages"]
    assert listed[0]["query"] == "is:unread"
    assert listed[0]["max_results"] == 5


def test_get_message_returns_the_decoded_body(full_access) -> None:
    container, _transport, email = full_access
    container.transport.seed_message("m1", body="the body text")
    detail = container.mailbox.get_message(message_id="m1", account_email=email, now=NOW)
    assert detail.body_text is not None
    assert detail.body_text.strip() == "the body text"
    assert detail.subject == "Original subject"
    assert detail.sender == "Someone <someone@example.com>"


# ----------------------------------------------------------------- modifying


def test_marking_read_removes_the_unread_label(full_access) -> None:
    container, transport, email = full_access
    transport.seed_message("m1")
    result = container.mailbox.modify_message(
        message_id="m1", states=("read",), account_email=email, now=NOW
    )
    assert LABEL_UNREAD not in result.label_ids
    assert LABEL_INBOX in result.label_ids


def test_starring_and_archiving_combine(full_access) -> None:
    container, transport, email = full_access
    transport.seed_message("m1")
    result = container.mailbox.modify_message(
        message_id="m1", states=("starred", "archived"), account_email=email, now=NOW
    )
    assert LABEL_STARRED in result.label_ids
    assert LABEL_INBOX not in result.label_ids


def test_an_unknown_state_is_refused_rather_than_ignored(full_access) -> None:
    container, _transport, email = full_access
    with pytest.raises(InvalidRequest) as excinfo:
        container.mailbox.modify_message(
            message_id="m1", states=("readed",), account_email=email, now=NOW
        )
    assert "unknown state" in str(excinfo.value)


def test_a_contradictory_label_request_resolves_in_favour_of_add(full_access) -> None:
    """Asking to add and remove the same label is a contradiction; the add wins, and it is not silent."""
    container, transport, email = full_access
    transport.seed_message("m1")
    container.mailbox.modify_message(
        message_id="m1",
        add_labels=(LABEL_STARRED,),
        remove_labels=(LABEL_STARRED,),
        account_email=email,
        now=NOW,
    )
    modify = next(c for c in transport.calls if c.get("op") == "modify_message")
    assert modify["add"] == (LABEL_STARRED,)
    assert modify["remove"] == ()


def test_modify_with_nothing_to_change_is_refused(full_access) -> None:
    container, _transport, email = full_access
    with pytest.raises(InvalidRequest) as excinfo:
        container.mailbox.modify_message(message_id="m1", account_email=email, now=NOW)
    assert "nothing to change" in str(excinfo.value)


# ------------------------------------------------------------------ replying


#: Long enough to clear the per-account pacing interval, so a second send is due.
LATER = NOW + timedelta(seconds=5)


def _drain(container, now: datetime = LATER) -> str:
    """Run the worker once and return the action. Enqueue then drain is the real send path.

    The default clock is past the pacing interval: a second send on the same account is scheduled
    `send_interval_seconds` out, so draining at `NOW` would correctly find nothing to do.
    """
    return Worker(container, worker_id="w", rand=lambda: 0.0).run_once(now=now).action


def _sent(container, job_id: int):
    """Read a job back after the worker has run, which is the only way to see a terminal status."""
    return container.queue.get(job_id)


def _send_and_record(container, transport, email, *, subject="Ping", body="hello") -> int:
    """Send a message and remember its job id, the way an agent would."""
    transport.script_result(message_id="outbound-1", thread_id="thread-out")
    outcome = container.sender.send(
        account_email=email,
        msg=_message(container, subject=subject, body=body),
        source="test",
        wait=False,
        now=NOW,
    )
    assert _drain(container) == "sent"
    return outcome.job_id


def _message(container, *, subject: str, body: str):
    from gmail_automator.gmail.mime import OutgoingMessage

    return OutgoingMessage(
        from_email="me@example.com", to=("them@example.com",), subject=subject, body=body
    )


def test_replying_to_a_sent_job_carries_the_original_headers(full_access) -> None:
    container, transport, email = full_access
    job_id = _send_and_record(container, transport, email)
    # The original the agent is answering.
    transport.seed_message(
        "outbound-1",
        thread_id="thread-out",
        subject="Ping",
        sender="me@example.com",
        message_id_header="<outbound-1@mail.example.com>",
        references="<root@x>",
    )
    transport.script_result(message_id="reply-1", thread_id="thread-out")
    outcome = container.replies.reply(
        body="the answer", job_id=job_id, account_email=email, wait=False, now=NOW
    )
    assert _drain(container) == "sent"
    assert _sent(container, outcome.job_id).status == "sent"

    sent = transport.calls[-1]
    raw = _decode(sent["raw_b64url"])
    assert raw["In-Reply-To"] == "<outbound-1@mail.example.com>"
    # The existing chain is extended with the id being answered, not replaced.
    assert raw["References"].split() == ["<root@x>", "<outbound-1@mail.example.com>"]
    assert raw["Subject"] == "Re: Ping"
    # Gmail's thread id is passed so the message threads in Gmail's own UI, not just by headers.
    assert sent["thread_id"] == "thread-out"


def test_replying_to_an_arbitrary_message_defaults_the_recipient_to_the_sender(full_access) -> None:
    container, transport, email = full_access
    transport.seed_message("inbound-9", subject="Question", sender="Them <them@example.com>")
    transport.script_result(message_id="reply-9", thread_id="t9")
    outcome = container.replies.reply(
        body="answer", message_id="inbound-9", account_email=email, wait=False, now=NOW
    )
    assert _drain(container) == "sent"
    assert _sent(container, outcome.job_id).status == "sent"
    raw = _decode(transport.calls[-1]["raw_b64url"])
    assert "them@example.com" in raw["To"]
    assert raw["Subject"] == "Re: Question"


def test_a_reply_never_doubles_the_re_prefix(full_access) -> None:
    container, transport, email = full_access
    transport.seed_message("inbound-9", subject="Re: Already", sender="them@example.com")
    transport.script_result(message_id="r", thread_id="t")
    container.replies.reply(
        body="a", message_id="inbound-9", account_email=email, wait=False, now=NOW
    )
    _drain(container)
    assert _decode(transport.calls[-1]["raw_b64url"])["Subject"] == "Re: Already"


def test_a_draft_reply_creates_a_draft_in_the_thread_and_sends_nothing(full_access) -> None:
    container, transport, email = full_access
    transport.seed_message(
        "inbound-9", thread_id="t9", subject="Question", sender="them@example.com"
    )
    result = container.replies.reply(
        body="a draft answer", message_id="inbound-9", draft=True, account_email=email, now=NOW
    )
    assert result.draft_id
    assert transport.draft_calls, "a draft reply must call create_draft"
    # Reading the original is expected; putting it on the wire is not.
    assert [c for c in transport.calls if "raw_b64url" in c] == [], "a draft must not send"
    assert transport.draft_calls[-1]["thread_id"] == "t9"


def test_replying_needs_exactly_one_target(full_access) -> None:
    container, _transport, email = full_access
    with pytest.raises(InvalidRequest) as neither:
        container.replies.reply(body="x", account_email=email, now=NOW)
    assert "exactly one" in str(neither.value)
    with pytest.raises(InvalidRequest):
        container.replies.reply(body="x", job_id=1, message_id="m", account_email=email, now=NOW)


def test_replying_to_a_queued_job_is_refused(full_access) -> None:
    """A job that has not been sent has no message to answer, and no id to thread to."""
    container, _transport, email = full_access
    outcome = container.sender.send(
        account_email=email,
        msg=_message(container, subject="x", body="y"),
        source="test",
        wait=False,
        now=NOW,
    )
    assert container.queue.get(outcome.job_id).status == "pending"
    with pytest.raises(InvalidRequest) as excinfo:
        container.replies.reply(body="x", job_id=outcome.job_id, account_email=email, now=NOW)
    assert "nothing to reply to" in str(excinfo.value)


def test_replying_to_an_unknown_job_is_refused(full_access) -> None:
    container, _transport, email = full_access
    with pytest.raises(InvalidRequest) as excinfo:
        container.replies.reply(body="x", job_id=987654, account_email=email, now=NOW)
    assert "no send job" in str(excinfo.value)


def test_replying_is_refused_on_a_send_only_account(send_only) -> None:
    container, transport, email = send_only
    with pytest.raises(ScopeMissing) as excinfo:
        container.replies.reply(body="x", message_id="m1", account_email=email, now=NOW)
    # The refusal comes from the mailbox service, which is where read access is enforced.
    assert "reading" in str(excinfo.value)
    assert transport.calls == []


def test_a_folded_references_header_cannot_inject_a_line_break(full_access) -> None:
    """The original arrives folded; the reply's References must be a single unfolded line."""
    container, transport, email = full_access
    transport.messages["folded"] = {
        "id": "folded",
        "threadId": "tf",
        "snippet": "",
        "labelIds": [],
        "headers": {
            "Message-ID": "<folded@x>",
            "From": "them@example.com",
            "Subject": "Folded",
            "References": "<a@x>\r\n <b@x>",
        },
        "raw_b64": "",
    }
    transport.script_result(message_id="r", thread_id="tf")
    container.replies.reply(body="a", message_id="folded", account_email=email, wait=False, now=NOW)
    _drain(container)
    raw = _decode(transport.calls[-1]["raw_b64url"])
    assert "\n" not in raw["References"]
    assert "\r" not in raw["References"]
    assert raw["References"].split() == ["<a@x>", "<b@x>", "<folded@x>"]
