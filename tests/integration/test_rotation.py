"""Encryption key rotation (Phase 3, P8).

R7 designs `TokenCipher` with `old_keys` precisely so a rotation can be staged: add the new key
alongside the old one, re-encrypt everything, then drop the old key. The order matters - dropping
the old key first makes every stored token unreadable.
"""

from __future__ import annotations

import base64
from datetime import UTC, datetime

import pytest

from gmail_automator.crypto import CryptoError, TokenCipher
from gmail_automator.models import Account
from gmail_automator.rotations import RotationPlan, TokenRotator, survey

KEY_A = bytes(range(32))
KEY_B = bytes(reversed(range(32)))
NOW = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
AAD = "me@example.com"


OLD = TokenCipher(KEY_A)


@pytest.fixture
def populated(accounts) -> Account:
    """An account whose tokens were written under the pre-rotation key."""
    return accounts.upsert_oauth_account(
        email="me@example.com",
        access_token_enc=OLD.encrypt("access-1", aad=AAD),
        refresh_token_enc=OLD.encrypt("refresh-1", aad=AAD),
        scopes=["https://www.googleapis.com/auth/gmail.send"],
        token_uri="http://oauth.test/token",
        now=NOW,
    )


def test_survey_counts_what_has_to_change(session_factory, populated) -> None:
    with session_factory() as session:
        plan = survey(session, old_cipher=TokenCipher(KEY_A), new_key=KEY_B, now=NOW)
    assert isinstance(plan, RotationPlan)
    assert plan.accounts_total == 1
    assert plan.accounts_with_tokens == 1
    assert plan.tokens_to_rotate == 2
    assert plan.undecryptable_accounts == ()


def test_survey_reports_an_undecryptable_account(session_factory, populated) -> None:
    """One corrupt value makes the whole account undecryptable, and is surfaced by address."""
    with session_factory() as session:
        row = session.get(Account, populated.id)
        row.access_token_enc = "v1.corrupt.value"
        session.commit()
    with session_factory() as session:
        plan = survey(session, old_cipher=TokenCipher(KEY_A), new_key=KEY_B, now=NOW)
    assert plan.undecryptable_accounts == ("me@example.com",)
    assert plan.accounts_with_tokens == 1


def test_rotation_rewrites_every_token(session_factory, populated) -> None:
    rotator = TokenRotator(
        session_factory=session_factory, old_cipher=TokenCipher(KEY_A), new_key=KEY_B, now=NOW
    )
    report = rotator.run()
    assert report.rotated == 2
    assert report.skipped == 0
    assert report.failed == ()

    with session_factory() as session:
        row = session.get(Account, populated.id)
        access, refresh = row.access_token_enc, row.refresh_token_enc

    new_cipher = TokenCipher(KEY_B)
    assert new_cipher.decrypt(access, aad=AAD) == "access-1"
    assert new_cipher.decrypt(refresh, aad=AAD) == "refresh-1"
    with pytest.raises(CryptoError):
        TokenCipher(KEY_A).decrypt(access, aad=AAD)


def test_rotation_is_idempotent(session_factory, populated) -> None:
    rotator = TokenRotator(
        session_factory=session_factory, old_cipher=TokenCipher(KEY_A), new_key=KEY_B, now=NOW
    )
    rotator.run()
    # the staged cipher the operator would actually use during the window
    second = TokenRotator(
        session_factory=session_factory,
        old_cipher=TokenCipher(KEY_A, old_keys=(KEY_B,)),
        new_key=KEY_B,
        now=NOW,
    ).run()
    assert second.skipped == 2
    assert second.rotated == 0
    assert second.failed == ()
    with session_factory() as session:
        row = session.get(Account, populated.id)
        assert TokenCipher(KEY_B).decrypt(row.access_token_enc, aad=AAD) == "access-1"


def test_a_failed_account_does_not_stop_the_others(session_factory, accounts) -> None:
    good = accounts.upsert_oauth_account(
        email="good@example.com",
        access_token_enc=OLD.encrypt("a", aad="good@example.com"),
        token_uri="http://oauth.test/token",
        now=NOW,
    )
    bad = accounts.upsert_oauth_account(
        email="bad@example.com",
        access_token_enc="v1.corrupt.value",
        token_uri="http://oauth.test/token",
        now=NOW,
    )
    report = TokenRotator(
        session_factory=session_factory, old_cipher=TokenCipher(KEY_A), new_key=KEY_B, now=NOW
    ).run()
    assert report.rotated == 1
    assert report.failed == ("bad@example.com",)
    with session_factory() as session:
        assert session.get(Account, good.id).access_token_enc is not None
        assert session.get(Account, bad.id).access_token_enc == "v1.corrupt.value"


def test_a_staged_rotation_needs_the_old_key_present(session_factory, cipher, populated) -> None:
    """A cipher with only the new key cannot read pre-rotation data - that is the whole point of
    keeping `old_keys` during the staged window."""
    with session_factory() as session:
        row = session.get(Account, populated.id)
        stored = row.access_token_enc
    with pytest.raises(CryptoError):
        TokenCipher(KEY_B).decrypt(stored, aad=AAD)
    assert TokenCipher(KEY_B, old_keys=(KEY_A,)).decrypt(stored, aad=AAD) == "access-1"


def test_base64_helper_round_trips() -> None:
    from gmail_automator.crypto import encode_key

    encoded = encode_key(KEY_B)
    assert isinstance(encoded, str)
    assert base64.urlsafe_b64decode(encoded + "===") == KEY_B
