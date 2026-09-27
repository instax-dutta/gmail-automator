"""Idempotent replay (PRD 6, reliability).

A client that retries after a timeout must not send twice. The first submission records a
fingerprint of the request; a matching retry returns the *original* job's outcome instead of
creating a second one.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from gmail_automator.api_keys import ApiKeyContext
from gmail_automator.errors import DuplicateRequest
from gmail_automator.gmail.client import SendResult
from gmail_automator.gmail.mime import OutgoingMessage
from gmail_automator.schemas import SendEmailRequest, to_outgoing_message
from gmail_automator.send import SendService, request_fingerprint

NOW = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
HASH_A = "a" * 64
HASH_B = "b" * 64


def _msg(subject: str = "s") -> OutgoingMessage:
    return OutgoingMessage(
        from_email="sender@example.com", to=("a@example.com",), subject=subject, body="b"
    )


def _send(sender: SendService, *, subject: str = "s", **kwargs) -> object:
    """A send carrying an idempotency key also carries the request fingerprint."""
    kwargs.setdefault("source", "api")
    kwargs.setdefault("wait", False)
    kwargs.setdefault("now", NOW)
    if kwargs.get("idempotency_key") is not None:
        kwargs.setdefault("request_hash", HASH_A)
    return sender.send(account_email=None, msg=_msg(subject), **kwargs)


def test_same_key_same_payload_replays_the_original_job(sender, account) -> None:
    first = _send(sender, idempotency_key="k1")
    replay = _send(sender, idempotency_key="k1")
    assert replay.job_id == first.job_id
    assert replay.status == "queued"


def test_a_replay_after_completion_reports_the_real_outcome(sender, queue, account) -> None:
    first = _send(sender, idempotency_key="k1")
    queue.mark_sent(
        job_id=first.job_id,
        result=SendResult(message_id="gmail-77", thread_id="t", label_ids=("SENT",)),
        now=NOW,
    )
    replay = _send(sender, idempotency_key="k1")
    assert replay.job_id == first.job_id
    assert replay.status == "sent"
    assert replay.message_id == "gmail-77"


def test_a_replay_after_failure_reports_the_failure(sender, queue, account) -> None:
    first = _send(sender, idempotency_key="k1")
    queue.mark_failed(
        job_id=first.job_id, error_code="daily_send_quota_exceeded", message="limit", now=NOW
    )
    replay = _send(sender, idempotency_key="k1")
    assert replay.status == "failed"
    assert replay.error_code == "daily_send_quota_exceeded"


def test_the_same_key_with_a_different_payload_is_rejected(sender, account) -> None:
    _send(sender, idempotency_key="k1", request_hash=HASH_A)
    with pytest.raises(DuplicateRequest) as excinfo:
        _send(sender, idempotency_key="k1", request_hash=HASH_B)
    assert "idempotency_key" in str(excinfo.value.details)


def test_a_key_reused_without_a_fingerprint_is_a_conflict_not_a_replay(sender, account) -> None:
    """A row written before fingerprints existed must not silently swallow a different request."""
    _send(sender, idempotency_key="k1", request_hash=HASH_A)
    with pytest.raises(DuplicateRequest):
        _send(sender, idempotency_key="k1", request_hash=None)


def test_keys_are_scoped_per_client(sender, session_factory, account) -> None:
    from gmail_automator.models import ApiKeyRow

    with session_factory() as session:
        for key_id in (1, 2):
            session.add(
                ApiKeyRow(
                    id=key_id,
                    name=f"k{key_id}",
                    key_prefix=f"fmg_k{key_id}",
                    key_hash="0" * 64,
                    scopes=["send", "read"],
                )
            )
        session.commit()
    first = _send(sender, idempotency_key="shared", api_key=ApiKeyContext(key_id=1, name="a"))
    second = _send(sender, idempotency_key="shared", api_key=ApiKeyContext(key_id=2, name="b"))
    assert first.job_id != second.job_id


def test_the_bootstrap_identity_shares_one_idempotency_namespace(sender, account) -> None:
    first = _send(sender, source="cli", idempotency_key="k1")
    replay = _send(sender, source="api", idempotency_key="k1")
    assert replay.job_id == first.job_id


def test_without_a_key_two_identical_sends_are_two_jobs(sender, account) -> None:
    first = _send(sender)
    second = _send(sender)
    assert first.job_id != second.job_id


def test_replay_never_creates_a_second_job(sender, queue, account) -> None:
    _send(sender, idempotency_key="k1")
    _send(sender, idempotency_key="k1")
    _send(sender, subject="other")
    assert queue.depth() == 2  # k1 once, plus the unrelated send


def test_replay_never_consumes_extra_quota(sender, quota, account) -> None:
    _send(sender, idempotency_key="k1")
    _send(sender, idempotency_key="k1")
    resolved = sender.accounts.resolve(None)
    snapshot = quota.snapshot(resolved, now=NOW)
    assert snapshot.pending_jobs == 1
    assert snapshot.pending_recipients == 1


# ------------------------------------------------------------ request hash


def test_request_hash_is_stable_for_equivalent_payloads() -> None:
    a = SendEmailRequest(to=["x@example.com"], subject="s", body="b")
    b = SendEmailRequest(to=["x@example.com"], subject="s", body="b", wait=False)
    assert request_fingerprint(a) == request_fingerprint(b)


def test_request_hash_ignores_transport_only_fields() -> None:
    """`thread_id` is Gmail transport metadata, not part of the message."""
    a = SendEmailRequest(to=["x@example.com"], subject="s", body="b")
    b = SendEmailRequest(to=["x@example.com"], subject="s", body="b", thread_id="t-1")
    assert request_fingerprint(a) == request_fingerprint(b)


def test_request_hash_changes_with_the_content() -> None:
    a = SendEmailRequest(to=["x@example.com"], subject="s", body="b")
    b = SendEmailRequest(to=["x@example.com"], subject="s", body="c")
    assert request_fingerprint(a) != request_fingerprint(b)


def test_request_hash_changes_with_recipient_order() -> None:
    a = SendEmailRequest(to=["x@example.com", "y@example.com"], subject="s", body="b")
    b = SendEmailRequest(to=["y@example.com", "x@example.com"], subject="s", body="b")
    assert request_fingerprint(a) != request_fingerprint(b)


def test_request_hash_changes_when_a_cc_is_added() -> None:
    a = SendEmailRequest(to=["x@example.com"], subject="s", body="b")
    b = SendEmailRequest(to=["x@example.com"], cc=["z@example.com"], subject="s", body="b")
    assert request_fingerprint(a) != request_fingerprint(b)


def test_request_hash_is_hex_and_bounded() -> None:
    digest = request_fingerprint(
        SendEmailRequest(to=["x@example.com"], subject="s", body="b" * 100_000)
    )
    assert len(digest) == 64
    assert all(char in "0123456789abcdef" for char in digest)


def test_request_hash_does_not_embed_the_body() -> None:
    secret = "super-secret-body-content"
    digest = request_fingerprint(SendEmailRequest(to=["x@example.com"], subject="s", body=secret))
    assert secret not in digest


def test_to_outgoing_message_is_built_before_the_hash_is_used() -> None:
    request = SendEmailRequest(to=["a@example.com"], subject="s", body="b")
    message = to_outgoing_message(request, "me@example.com")
    assert message.to == ("a@example.com",)
