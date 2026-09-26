from datetime import UTC, datetime, timedelta

import pytest

from fmaiily.accounts import AccountService
from fmaiily.api_keys import ApiKeyContext, bearer_token_from_header
from fmaiily.crypto import TokenCipher
from fmaiily.errors import (
    AccountNotFound,
    AttachmentTooLarge,
    DuplicateRequest,
    Forbidden,
    InvalidRequest,
    QuotaExceeded,
    Unauthorized,
)
from fmaiily.gmail.client import SendResult
from fmaiily.gmail.mime import Attachment, OutgoingMessage
from fmaiily.history import HistoryService
from fmaiily.models import Account, ApiKeyRow, SendJob
from fmaiily.queue import QueueService
from fmaiily.quota import QuotaService
from fmaiily.send import SendService

NOW = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
SENDER = "me@example.com"


def _msg(**overrides) -> OutgoingMessage:
    base = {
        "from_email": SENDER,
        "to": ("a@example.com",),
        "subject": "hi",
        "body": "hello",
    }
    return OutgoingMessage(**{**base, **overrides})


@pytest.fixture
def cipher(settings) -> TokenCipher:
    return TokenCipher(settings.encryption_key_bytes())


@pytest.fixture
def accounts(session_factory, seeded_engine, fake_clock, settings) -> AccountService:
    return AccountService(session_factory=session_factory, clock=fake_clock, settings=settings)


@pytest.fixture
def account(accounts: AccountService) -> Account:
    return accounts.upsert_oauth_account(
        email=SENDER,
        scopes=["https://www.googleapis.com/auth/gmail.send"],
        token_uri="https://oauth2.googleapis.com/token",
        now=NOW,
    )


@pytest.fixture
def quota(accounts, fake_clock) -> QuotaService:
    return QuotaService(session_factory=accounts.session_factory, clock=fake_clock)


@pytest.fixture
def queue(accounts, cipher, fake_clock, settings, sleeper) -> QueueService:
    return QueueService(
        session_factory=accounts.session_factory,
        cipher=cipher,
        clock=fake_clock,
        settings=settings,
        sleeper=sleeper,
    )


@pytest.fixture
def sender(settings, accounts, quota, queue, fake_clock, sleeper) -> SendService:
    return SendService(
        settings=settings,
        accounts=accounts,
        quota=quota,
        queue=queue,
        clock=fake_clock,
        sleeper=sleeper,
    )


@pytest.fixture
def history(accounts, fake_clock) -> HistoryService:
    return HistoryService(session_factory=accounts.session_factory, clock=fake_clock)


def _api_key_row(session_factory, name: str, key_id: int) -> ApiKeyContext:
    """API keys are foreign-keyed to `api_keys`; create the row a real key would own."""
    with session_factory() as session:
        session.add(
            ApiKeyRow(
                id=key_id,
                name=name,
                key_prefix=f"fmg_{name[:4]}",
                key_hash="0" * 64,
                scopes=["send", "read", "admin"],
            )
        )
        session.commit()
    return ApiKeyContext(key_id=key_id, name=name)


def _sent_job(session_factory, account: Account, *, at: datetime, recipients: int = 1) -> None:
    with session_factory() as session:
        session.add(
            SendJob(
                account_id=account.id,
                status="sent",
                recipients=recipients,
                source="api",
                scheduled_at=at,
                sent_at=at,
                created_at=at,
                updated_at=at,
            )
        )
        session.commit()


# ------------------------------------------------------------------ happy path


def test_send_enqueues_and_reports_queued(sender: SendService, account: Account) -> None:
    outcome = sender.send(account_email=None, msg=_msg(), source="api", wait=False, now=NOW)
    assert outcome.status == "queued"
    assert outcome.job_id > 0
    assert outcome.account_email == SENDER
    assert outcome.error_code is None
    assert outcome.quota is not None
    assert outcome.quota.pending_jobs == 1


def test_send_uses_the_only_connected_account(sender: SendService, account: Account) -> None:
    outcome = sender.send(account_email=None, msg=_msg(), source="mcp", wait=False, now=NOW)
    assert outcome.account_email == SENDER


def test_queued_payload_is_readable_by_the_worker(
    sender: SendService, queue: QueueService, account: Account
) -> None:
    outcome = sender.send(account_email=None, msg=_msg(), source="api", wait=False, now=NOW)
    payload = queue.decrypt_payload(queue.get(outcome.job_id))
    assert b"Subject: hi" in payload
    assert b"hello" in payload


def test_finished_job_is_reported_as_sent_with_the_gmail_id(
    sender: SendService, queue: QueueService, account: Account, quota: QuotaService
) -> None:
    outcome = sender.send(account_email=None, msg=_msg(), source="api", wait=False, now=NOW)
    queue.mark_sent(
        job_id=outcome.job_id,
        result=SendResult(message_id="gmail-9", thread_id="t", label_ids=("SENT",)),
        now=NOW,
    )
    reported = sender.outcome_for(
        queue.get(outcome.job_id), account=account, fallback_job_id=outcome.job_id, now=NOW
    )
    assert reported.status == "sent"
    assert reported.message_id == "gmail-9"
    assert reported.error_code is None


def test_failed_job_is_reported_as_failed_with_its_code(
    sender: SendService, queue: QueueService, account: Account
) -> None:
    outcome = sender.send(account_email=None, msg=_msg(), source="api", wait=False, now=NOW)
    queue.mark_failed(
        job_id=outcome.job_id,
        error_code="daily_send_quota_exceeded",
        message="daily limit reached",
        now=NOW,
    )
    reported = sender.outcome_for(
        queue.get(outcome.job_id), account=account, fallback_job_id=outcome.job_id, now=NOW
    )
    assert reported.status == "failed"
    assert reported.error_code == "daily_send_quota_exceeded"
    assert reported.message_id is None


def test_recipient_counts_include_cc_and_bcc(sender: SendService, account: Account) -> None:
    outcome = sender.send(
        account_email=None,
        msg=_msg(to=("a@x.com",), cc=("b@x.com",), bcc=("c@x.com",)),
        source="api",
        wait=False,
        now=NOW,
    )
    assert outcome.quota is not None
    assert outcome.quota.pending_recipients == 3


# ------------------------------------------------------------------ refusals


def test_send_rejects_a_revoked_account(sender: SendService, accounts, account: Account) -> None:
    accounts.revoke(SENDER)
    with pytest.raises(Forbidden) as excinfo:
        sender.send(account_email=None, msg=_msg(), source="api", wait=False, now=NOW)
    assert excinfo.value.details["status"] == "revoked"


def test_send_rejects_an_unknown_account(sender: SendService, account: Account) -> None:
    with pytest.raises(AccountNotFound):
        sender.send(
            account_email="nobody@example.com", msg=_msg(), source="api", wait=False, now=NOW
        )


def test_send_rejects_an_empty_recipient_list(sender: SendService, account: Account) -> None:
    with pytest.raises(InvalidRequest):
        sender.send(account_email=None, msg=_msg(to=()), source="api", wait=False, now=NOW)


def test_send_rejects_too_many_recipients(sender: SendService, account: Account) -> None:
    recipients = tuple(f"r{i}@example.com" for i in range(501))
    with pytest.raises(InvalidRequest) as excinfo:
        sender.send(account_email=None, msg=_msg(to=recipients), source="api", wait=False, now=NOW)
    assert excinfo.value.details["max_recipients_per_message"] == 500


def test_send_rejects_an_oversized_body(sender: SendService, account: Account, settings) -> None:
    big = "x" * (settings.max_body_bytes + 1)
    with pytest.raises(InvalidRequest) as excinfo:
        sender.send(account_email=None, msg=_msg(body=big), source="api", wait=False, now=NOW)
    assert excinfo.value.details["max_body_bytes"] == settings.max_body_bytes


def test_send_rejects_attachments_when_disabled(sender: SendService, account: Account) -> None:
    msg = _msg(attachments=(Attachment("a.txt", b"x"),))
    with pytest.raises(InvalidRequest) as excinfo:
        sender.send(account_email=None, msg=msg, source="api", wait=False, now=NOW)
    assert "FMAIILY_ATTACHMENTS_ENABLED" in excinfo.value.message


def test_send_rejects_an_oversized_attachment(
    sender: SendService, account: Account, settings
) -> None:
    sender.settings = settings.model_copy(update={"attachments_enabled": True})
    oversized = Attachment("a.bin", b"0" * (settings.attachment_max_bytes + 1))
    with pytest.raises(AttachmentTooLarge):
        sender.send(
            account_email=None,
            msg=_msg(attachments=(oversized,)),
            source="api",
            wait=False,
            now=NOW,
        )


def test_send_refuses_when_the_quota_would_be_crossed(
    sender: SendService, account: Account, session_factory
) -> None:
    for _ in range(425):
        _sent_job(session_factory, account, at=NOW)
    with pytest.raises(QuotaExceeded):
        sender.send(account_email=None, msg=_msg(), source="api", wait=False, now=NOW)


def test_a_refused_send_leaves_no_job_behind(
    sender: SendService, queue: QueueService, account: Account, settings
) -> None:
    with pytest.raises(InvalidRequest):
        sender.send(
            account_email=None,
            msg=_msg(body="x" * (settings.max_body_bytes + 1)),
            source="api",
            wait=False,
            now=NOW,
        )
    assert queue.depth() == 0


# --------------------------------------------------------------- permissions


def test_api_key_without_send_scope_is_refused(sender: SendService, account: Account) -> None:
    key = ApiKeyContext(key_id=1, name="read-only", scopes=("read",))
    with pytest.raises(Forbidden) as excinfo:
        sender.send(account_email=None, msg=_msg(), source="api", api_key=key, wait=False, now=NOW)
    assert excinfo.value.details["required_scope"] == "send"


def test_api_key_restricted_to_other_accounts_is_refused(
    sender: SendService, account: Account
) -> None:
    key = ApiKeyContext(key_id=2, name="other", allowed_accounts=("someone@else.com",))
    with pytest.raises(Forbidden):
        sender.send(account_email=None, msg=_msg(), source="api", api_key=key, wait=False, now=NOW)


def test_permitted_api_key_records_its_id_on_the_job(
    sender: SendService, queue: QueueService, account: Account, session_factory
) -> None:
    key = _api_key_row(session_factory, "agent", 7)
    outcome = sender.send(
        account_email=None, msg=_msg(), source="api", api_key=key, wait=False, now=NOW
    )
    assert queue.get(outcome.job_id).api_key_id == 7


def test_bootstrap_identity_has_no_api_key_row(
    sender: SendService, queue: QueueService, account: Account
) -> None:
    outcome = sender.send(
        account_email=None,
        msg=_msg(),
        source="cli",
        api_key=ApiKeyContext.local(),
        wait=False,
        now=NOW,
    )
    assert queue.get(outcome.job_id).api_key_id is None


# ----------------------------------------------------------------- idempotency


def test_duplicate_idempotency_key_is_rejected(sender: SendService, account: Account) -> None:
    sender.send(
        account_email=None, msg=_msg(), source="api", idempotency_key="abc", wait=False, now=NOW
    )
    with pytest.raises(DuplicateRequest):
        sender.send(
            account_email=None,
            msg=_msg(),
            source="api",
            idempotency_key="abc",
            wait=False,
            now=NOW,
        )


def test_idempotency_keys_are_scoped_per_client(
    sender: SendService, account: Account, session_factory
) -> None:
    first = sender.send(
        account_email=None,
        msg=_msg(),
        source="api",
        api_key=_api_key_row(session_factory, "alpha", 1),
        idempotency_key="abc",
        wait=False,
        now=NOW,
    )
    second = sender.send(
        account_email=None,
        msg=_msg(),
        source="api",
        api_key=_api_key_row(session_factory, "bravo", 2),
        idempotency_key="abc",
        wait=False,
        now=NOW,
    )
    assert first.job_id != second.job_id


# ------------------------------------------------------------------ waiting


def test_wait_times_out_as_queued_when_no_worker_runs(
    sender: SendService, account: Account, sleeper
) -> None:
    outcome = sender.send(
        account_email=None, msg=_msg(), source="api", wait=True, wait_timeout=1.0, now=NOW
    )
    assert outcome.status == "queued"
    assert sleeper.slept  # it really polled rather than blocking


# ------------------------------------------------------------------- batches


def test_send_batch_returns_one_outcome_per_message(sender: SendService, account: Account) -> None:
    batch = sender.send_batch(
        account_email=None,
        messages=[_msg(subject=f"s{i}") for i in range(3)],
        source="api",
        wait=False,
        now=NOW,
    )
    assert len(batch.outcomes) == 3
    assert all(o.status == "queued" for o in batch.outcomes)
    assert len({o.job_id for o in batch.outcomes}) == 3
    assert batch.failed == 0
    assert batch.sent == 0


def test_send_batch_requires_at_least_one_message(sender: SendService, account: Account) -> None:
    with pytest.raises(InvalidRequest):
        sender.send_batch(account_email=None, messages=[], source="api", now=NOW)


def test_send_batch_propagates_a_preflight_refusal(
    sender: SendService, queue: QueueService, account: Account
) -> None:
    with pytest.raises(InvalidRequest):
        sender.send_batch(
            account_email=None,
            messages=[_msg(), _msg(to=tuple(f"r{i}@x.com" for i in range(501)))],
            source="api",
            wait=False,
            now=NOW,
        )
    assert queue.depth() == 0  # nothing was queued by the refused batch


# ------------------------------------------------------------------ api keys


def test_bearer_token_parsing() -> None:
    assert bearer_token_from_header("Bearer fmg_abc") == "fmg_abc"
    assert bearer_token_from_header("bearer  fmg_abc ") == "fmg_abc"
    with pytest.raises(Unauthorized):
        bearer_token_from_header(None)
    with pytest.raises(Unauthorized):
        bearer_token_from_header("Basic abc")
    with pytest.raises(Unauthorized):
        bearer_token_from_header("Bearer")


def test_local_context_is_fully_permitted() -> None:
    key = ApiKeyContext.local()
    key.require_scope("send")
    key.require_scope("admin")
    assert key.allows_account("anything@example.com")
    assert key.key_id is None


def test_api_key_scope_check_is_exact() -> None:
    key = ApiKeyContext(key_id=1, name="k", scopes=("send",))
    assert key.has_scope("send")
    assert not key.has_scope("admin")
    with pytest.raises(Forbidden):
        key.require_scope("admin")


def test_api_key_account_allowlist_is_case_insensitive() -> None:
    key = ApiKeyContext(key_id=1, name="k", allowed_accounts=("Me@Example.com",))
    assert key.allows_account("me@example.com")
    assert not key.allows_account("other@example.com")


# ------------------------------------------------------------------ history


def test_history_lists_recent_sends(sender: SendService, account: Account, history) -> None:
    sender.send(account_email=None, msg=_msg(), source="api", wait=False, now=NOW)
    items = history.list_recent()
    assert len(items) == 1
    assert items[0].account == SENDER
    assert items[0].status == "pending"
    assert items[0].recipients == 1


def test_history_is_newest_first_and_respects_the_limit(
    sender: SendService, account: Account, history
) -> None:
    for i in range(3):
        sender.send(
            account_email=None, msg=_msg(subject=f"s{i}"), source="api", wait=False, now=NOW
        )
    items = history.list_recent(limit=2)
    assert [i.job_id for i in items] == sorted([i.job_id for i in items], reverse=True)
    assert len(items) == 2


def test_history_filters_by_status(sender: SendService, account: Account, history) -> None:
    sender.send(account_email=None, msg=_msg(), source="api", wait=False, now=NOW)
    assert history.list_recent(status="sent") == []
    assert len(history.list_recent(status="pending")) == 1


def test_history_filters_by_account(
    sender: SendService, accounts, account: Account, history
) -> None:
    accounts.upsert_oauth_account(
        email="other@example.com", token_uri="https://oauth2.googleapis.com/token", now=NOW
    )
    # with two accounts connected the caller must name one
    with pytest.raises(InvalidRequest):
        sender.send(account_email=None, msg=_msg(), source="api", wait=False, now=NOW)
    sender.send(account_email=SENDER, msg=_msg(), source="api", wait=False, now=NOW)
    sender.send(account_email="other@example.com", msg=_msg(), source="api", wait=False, now=NOW)
    assert len(history.list_recent()) == 2
    assert len(history.list_recent(account_email="other@example.com")) == 1


def test_job_status_view(sender: SendService, account: Account, history) -> None:
    outcome = sender.send(account_email=None, msg=_msg(), source="api", wait=False, now=NOW)
    status = history.job_status(outcome.job_id)
    assert status is not None
    assert status.account == SENDER
    assert status.attempts == 0
    assert status.sent_at is None
    assert history.job_status(9999) is None


def test_purge_removes_only_old_terminal_rows(
    sender: SendService, account: Account, history, session_factory
) -> None:
    _sent_job(session_factory, account, at=NOW - timedelta(days=90))
    sender.send(account_email=None, msg=_msg(), source="api", wait=False, now=NOW)
    assert history.purge_older_than(days=30, now=NOW) == 1
    assert len(history.list_recent()) == 1
