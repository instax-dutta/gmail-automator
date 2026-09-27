from datetime import UTC, datetime, timedelta

import pytest

from gmail_automator.accounts import AccountService
from gmail_automator.errors import AccountNotFound, InvalidRequest
from gmail_automator.models import Account

NOW = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)


@pytest.fixture
def accounts(session_factory, seeded_engine, fake_clock, settings) -> AccountService:
    return AccountService(session_factory=session_factory, clock=fake_clock, settings=settings)


def _connect(accounts: AccountService, email: str = "me@example.com", **overrides) -> Account:
    payload = {
        "email": email,
        "account_type": "personal",
        "access_token_enc": "v1.aaa.bbb",
        "refresh_token_enc": "v1.ccc.ddd",
        "expiry": NOW + timedelta(hours=1),
        "scopes": ["https://www.googleapis.com/auth/gmail.send"],
        "token_uri": "https://oauth2.googleapis.com/token",
    }
    return accounts.upsert_oauth_account(**{**payload, **overrides})


def test_upsert_creates_account_with_settings_defaults(accounts: AccountService) -> None:
    account = _connect(accounts)
    assert account.id is not None
    assert account.email == "me@example.com"
    assert account.status == "active"
    assert account.auth_type == "oauth"
    assert account.daily_message_limit == 500
    assert account.daily_recipient_limit == 500
    assert account.soft_limit_ratio == pytest.approx(0.85)
    assert account.send_interval_seconds == pytest.approx(2.0)
    assert account.next_send_at is None
    assert account.token_expiry == NOW + timedelta(hours=1)


def test_upsert_updates_existing_account_without_duplicating(accounts: AccountService) -> None:
    first = _connect(accounts)
    second = _connect(accounts, access_token_enc="v1.new.new", account_type="workspace")
    assert second.id == first.id
    assert accounts.list_all().__len__() == 1
    assert accounts.get("me@example.com").access_token_enc == "v1.new.new"
    assert accounts.get("me@example.com").account_type == "workspace"


def test_email_lookup_is_case_insensitive(accounts: AccountService) -> None:
    _connect(accounts)
    assert accounts.get("ME@Example.COM").id is not None


def test_get_missing_account_raises(accounts: AccountService) -> None:
    with pytest.raises(AccountNotFound) as excinfo:
        accounts.get("nobody@example.com")
    assert excinfo.value.details["account"] == "nobody@example.com"


def test_get_by_id(accounts: AccountService) -> None:
    account = _connect(accounts)
    assert accounts.get_by_id(account.id).email == "me@example.com"
    with pytest.raises(AccountNotFound):
        accounts.get_by_id(9999)


def test_resolve_single_active_account_when_email_is_none(accounts: AccountService) -> None:
    _connect(accounts)
    assert accounts.resolve(None).email == "me@example.com"


def test_resolve_without_any_account_raises(accounts: AccountService) -> None:
    with pytest.raises(AccountNotFound):
        accounts.resolve(None)


def test_resolve_is_ambiguous_with_multiple_accounts(accounts: AccountService) -> None:
    _connect(accounts, "a@example.com")
    _connect(accounts, "b@example.com")
    with pytest.raises(InvalidRequest) as excinfo:
        accounts.resolve(None)
    assert "account" in excinfo.value.message
    assert accounts.resolve("b@example.com").email == "b@example.com"


def test_revoked_accounts_are_not_candidates_for_resolution(accounts: AccountService) -> None:
    _connect(accounts, "a@example.com")
    _connect(accounts, "b@example.com")
    accounts.revoke("b@example.com")
    assert accounts.resolve(None).email == "a@example.com"
    assert [a.email for a in accounts.list_active()] == ["a@example.com"]


def test_revoke_clears_tokens_and_marks_status(accounts: AccountService) -> None:
    _connect(accounts)
    accounts.revoke("me@example.com")
    account = accounts.get("me@example.com")
    assert account.status == "revoked"
    assert account.access_token_enc is None
    assert account.refresh_token_enc is None
    assert account.token_expiry is None


def test_revoke_missing_account_raises(accounts: AccountService) -> None:
    with pytest.raises(AccountNotFound):
        accounts.revoke("nobody@example.com")


def test_set_status_records_refresh_error(accounts: AccountService) -> None:
    _connect(accounts)
    accounts.set_status("me@example.com", "error", error="invalid_grant")
    account = accounts.get("me@example.com")
    assert account.status == "error"
    assert account.last_refresh_error == "invalid_grant"


def test_record_refresh_success_clears_error(accounts: AccountService) -> None:
    _connect(accounts)
    accounts.set_status("me@example.com", "error", error="invalid_grant")
    accounts.record_refresh(
        "me@example.com", access_token_enc="v1.fresh.fresh", expiry=NOW + timedelta(hours=1)
    )
    account = accounts.get("me@example.com")
    assert account.access_token_enc == "v1.fresh.fresh"
    assert account.token_expiry == NOW + timedelta(hours=1)
    assert account.last_refresh_at == NOW
    assert account.last_refresh_error is None
    assert account.status == "active"


def test_record_refresh_failure_keeps_previous_token(accounts: AccountService) -> None:
    _connect(accounts)
    accounts.record_refresh("me@example.com", error="temporarily_unavailable")
    account = accounts.get("me@example.com")
    assert account.access_token_enc == "v1.aaa.bbb"
    assert account.last_refresh_error == "temporarily_unavailable"


def test_advance_pacing_sets_next_send_at(accounts: AccountService) -> None:
    _connect(accounts)
    accounts.advance_pacing("me@example.com", next_send_at=NOW + timedelta(seconds=2))
    assert accounts.get("me@example.com").next_send_at == NOW + timedelta(seconds=2)


def test_list_all_includes_revoked(accounts: AccountService) -> None:
    _connect(accounts, "a@example.com")
    _connect(accounts, "b@example.com")
    accounts.revoke("b@example.com")
    assert {a.email for a in accounts.list_all()} == {"a@example.com", "b@example.com"}


def test_summaries_expose_operator_fields(accounts: AccountService) -> None:
    _connect(accounts)
    summary = accounts.summaries()[0]
    assert summary.email == "me@example.com"
    assert summary.status == "active"
    assert summary.account_type == "personal"
    assert summary.scopes == ["https://www.googleapis.com/auth/gmail.send"]


def test_timestamps_are_returned_as_aware_utc(accounts: AccountService) -> None:
    account = _connect(accounts)
    loaded = accounts.get("me@example.com")
    assert loaded.created_at.tzinfo is UTC
    assert loaded.updated_at.tzinfo is UTC
    assert account.id == loaded.id
