"""Encryption key rotation (Phase 3, P8).

A staged rotation is three steps, in this order:

1. Set `FMAIILY_TOKEN_ENCRYPTION_KEY` to the new key **and** the previous one to
   `FMAIILY_TOKEN_ENCRYPTION_KEY_OLD`. The gateway now reads both and writes only the new one.
2. `fmaiily rotate-keys` re-encrypts every stored token.
3. Remove `FMAIILY_TOKEN_ENCRYPTION_KEY_OLD`.

Doing step 3 first, or step 2 without step 1, makes every stored token unreadable - which for an
OAuth refresh token means reconnecting every account by hand.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from fmaiily.crypto import CryptoError, TokenCipher
from fmaiily.models import Account


def encode_key(key: bytes) -> str:
    """Render a raw key the way the environment variable expects it."""
    return base64.urlsafe_b64encode(key).decode().rstrip("=")


@dataclass(frozen=True)
class RotationPlan:
    accounts_total: int
    accounts_with_tokens: int
    tokens_to_rotate: int
    already_rotated: int
    undecryptable_accounts: tuple[str, ...] = ()


@dataclass(frozen=True)
class RotationReport:
    rotated: int
    skipped: int
    failed: tuple[str, ...] = ()


def survey(
    session: Session, *, old_cipher: TokenCipher, new_key: bytes, now: datetime
) -> RotationPlan:
    """Report what a rotation would touch, without writing anything.

    Run this first: it is the only way to find out that some account is already unreadable before
    you depend on the rotation succeeding.

    "Does it need rotating?" is answered by a cipher holding *only* the new key, because the
    question is whether the value will still be readable once the old key is dropped. A staged
    cipher, which still accepts old material, would answer yes to everything.
    """
    new_only = TokenCipher(new_key)
    accounts = list(session.scalars(select(Account).order_by(Account.id)))
    with_tokens = 0
    to_rotate = 0
    already = 0
    undecryptable: list[str] = []

    for account in accounts:
        stored = [account.access_token_enc, account.refresh_token_enc]
        present = [value for value in stored if value]
        if not present:
            continue
        with_tokens += 1
        for value in present:
            try:
                old_cipher.decrypt(value, aad=account.email)
            except CryptoError:
                undecryptable.append(account.email)
                break
            try:
                new_only.decrypt(value, aad=account.email)
            except CryptoError:
                to_rotate += 1
            else:
                already += 1
    _ = now
    return RotationPlan(
        accounts_total=len(accounts),
        accounts_with_tokens=with_tokens,
        tokens_to_rotate=to_rotate,
        already_rotated=already,
        undecryptable_accounts=tuple(sorted(set(undecryptable))),
    )


@dataclass
class TokenRotator:
    """Re-encrypts stored tokens under a new key, one account per transaction.

    One account failing never rolls back the others: a single corrupt row must not leave the whole
    estate on the old key.
    """

    session_factory: sessionmaker[Session]
    old_cipher: TokenCipher
    new_key: bytes
    now: datetime
    _new_cipher: TokenCipher = field(init=False)
    _new_key_only: TokenCipher = field(init=False)

    def __post_init__(self) -> None:
        # Two ciphers on purpose: writes use a staged cipher so a partially rotated estate stays
        # readable, while the "already done?" check uses the new key alone, because that is what
        # answers whether the value will survive dropping the old key.
        self._new_cipher = TokenCipher(self.new_key, old_keys=self.old_cipher.known_keys)
        self._new_key_only = TokenCipher(self.new_key)

    def run(self) -> RotationReport:
        rotated = 0
        skipped = 0
        failed: list[str] = []
        with self.session_factory() as session:
            account_ids = list(session.scalars(select(Account.id).order_by(Account.id)))

        for account_id in account_ids:
            outcome, rotated_here, considered = self._rotate_one(account_id)
            if outcome == "failed":
                failed.append(_email_for(self.session_factory, account_id) or str(account_id))
            rotated += rotated_here
            skipped += considered - rotated_here
        return RotationReport(rotated=rotated, skipped=skipped, failed=tuple(failed))

    def _rotate_one(self, account_id: int) -> tuple[str, int, int]:
        """Return `(outcome, rotated, considered)` for one account, committed independently."""
        with self.session_factory() as session:
            account = session.get(Account, account_id)
            if account is None:
                return "missing", 0, 0
            changes = 0
            considered = 0
            for field_name in ("access_token_enc", "refresh_token_enc"):
                value = getattr(account, field_name)
                if not value:
                    continue
                considered += 1
                try:
                    plaintext = self.old_cipher.decrypt(value, aad=account.email)
                except CryptoError:
                    # Unreadable under the old key: leave the value untouched and report the
                    # account, rather than writing something the operator cannot recover.
                    session.rollback()
                    return "failed", 0, considered
                try:
                    self._new_key_only.decrypt(value, aad=account.email)
                except CryptoError:
                    setattr(
                        account, field_name, self._new_cipher.encrypt(plaintext, aad=account.email)
                    )
                    changes += 1
            if changes:
                account.updated_at = self.now
                session.commit()
            else:
                session.rollback()
            return ("rotated" if changes else "skipped"), changes, considered


def _email_for(session_factory: sessionmaker[Session], account_id: int) -> str | None:
    with session_factory() as session:
        account = session.get(Account, account_id)
        return account.email if account is not None else None


__all__ = ["RotationPlan", "RotationReport", "TokenRotator", "encode_key", "survey"]
