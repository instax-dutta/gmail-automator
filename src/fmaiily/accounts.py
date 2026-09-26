from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from fmaiily.clock import Clock
from fmaiily.config import Settings
from fmaiily.errors import AccountNotFound, InvalidRequest
from fmaiily.models import Account
from fmaiily.schemas import AccountSummary


class AccountService:
    """Read/write access to connected Gmail accounts.

    Every method opens its own short-lived session, so the service is safe to call from the
    FastAPI threadpool, the MCP thread bridge, and worker threads at the same time.
    """

    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        clock: Clock,
        settings: Settings,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock
        self._settings = settings

    @property
    def session_factory(self) -> sessionmaker[Session]:
        return self._session_factory

    @property
    def settings(self) -> Settings:
        return self._settings

    # ------------------------------------------------------------------ writes

    def upsert_oauth_account(
        self,
        *,
        email: str,
        account_type: str = "personal",
        access_token_enc: str | None = None,
        refresh_token_enc: str | None = None,
        expiry: datetime | None = None,
        scopes: list[str] | None = None,
        token_uri: str,
        now: datetime | None = None,
    ) -> Account:
        now = now or self._clock.now()
        normalized = email.strip().lower()
        with self._session_factory() as session:
            account = session.scalar(select(Account).where(Account.email == normalized))
            if account is None:
                account = Account(
                    email=normalized,
                    token_uri=token_uri,
                    created_at=now,
                    updated_at=now,
                )
                session.add(account)
            account.auth_type = "oauth"
            account.account_type = account_type
            account.token_uri = token_uri
            account.status = "active"
            account.updated_at = now
            if access_token_enc is not None:
                account.access_token_enc = access_token_enc
            # Google only returns a refresh token on the first consent for some account types;
            # a later connect without one must not wipe the token we already hold.
            if refresh_token_enc is not None:
                account.refresh_token_enc = refresh_token_enc
            if expiry is not None:
                account.token_expiry = expiry
            if scopes is not None:
                account.scopes = list(scopes)
            self._apply_limit_defaults(account)
            session.commit()
            session.refresh(account)
            session.expunge(account)
        return account

    def register_service_account(
        self,
        *,
        email: str,
        scopes: list[str],
        now: datetime | None = None,
    ) -> Account:
        """Register a Workspace mailbox reached through domain-wide delegation.

        No token is stored: a service account mints one on demand, so there is no refresh secret to
        protect. The quota and pacing columns are set exactly as for an OAuth account, because
        Gmail applies the same limits to impersonated sends.
        """
        now = now or self._clock.now()
        normalized = email.strip().lower()
        with self._session_factory() as session:
            account = session.scalar(select(Account).where(Account.email == normalized))
            if account is None:
                account = Account(
                    email=normalized,
                    token_uri="",
                    created_at=now,
                    updated_at=now,
                )
                session.add(account)
            account.auth_type = "service_account"
            account.account_type = "workspace"
            account.token_uri = ""
            account.status = "active"
            account.scopes = list(scopes)
            account.access_token_enc = None
            account.refresh_token_enc = None
            account.token_expiry = None
            account.updated_at = now
            self._apply_limit_defaults(account)
            session.commit()
            session.refresh(account)
            session.expunge(account)
        return account

    def revoke(self, email: str) -> None:
        now = self._clock.now()
        with self._session_factory() as session:
            account = self._require(session, email)
            account.status = "revoked"
            account.access_token_enc = None
            account.refresh_token_enc = None
            account.token_expiry = None
            account.last_refresh_at = now
            account.updated_at = now
            session.commit()

    def set_status(self, email: str, status: str, *, error: str | None = None) -> None:
        with self._session_factory() as session:
            account = self._require(session, email)
            account.status = status
            account.last_refresh_error = error
            account.updated_at = self._clock.now()
            session.commit()

    def record_refresh(
        self,
        email: str,
        *,
        access_token_enc: str | None = None,
        refresh_token_enc: str | None = None,
        expiry: datetime | None = None,
        error: str | None = None,
    ) -> None:
        """Persist a refresh outcome in a single write.

        A failure keeps the previous token so a transient Google outage does not destroy a working
        session, and flips the account to `error` so an operator can see it. The next successful
        refresh flips it back to `active`.
        """
        now = self._clock.now()
        with self._session_factory() as session:
            account = self._require(session, email)
            account.last_refresh_at = now
            account.updated_at = now
            account.last_refresh_error = error
            if error is None:
                if access_token_enc is not None:
                    account.access_token_enc = access_token_enc
                if refresh_token_enc is not None:
                    account.refresh_token_enc = refresh_token_enc
                if expiry is not None:
                    account.token_expiry = expiry
                account.status = "active"
            else:
                account.status = "error"
            session.commit()

    def expire_token(self, email: str) -> None:
        """Force the next send to refresh: used when Gmail answers 401 (R: AuthExpired)."""
        with self._session_factory() as session:
            account = self._require(session, email)
            account.token_expiry = None
            account.updated_at = self._clock.now()
            session.commit()

    def advance_pacing(self, email: str, *, next_send_at: datetime) -> None:
        with self._session_factory() as session:
            account = self._require(session, email)
            account.next_send_at = next_send_at
            account.updated_at = self._clock.now()
            session.commit()

    # ------------------------------------------------------------------- reads

    def get(self, email: str) -> Account:
        with self._session_factory() as session:
            return self._require(session, email)

    def get_by_id(self, account_id: int) -> Account:
        with self._session_factory() as session:
            account = session.get(Account, account_id)
            if account is None:
                raise AccountNotFound("account not found", details={"account_id": account_id})
            session.expunge(account)
            return account

    def list_all(self) -> list[Account]:
        with self._session_factory() as session:
            accounts = list(session.scalars(select(Account).order_by(Account.email)))
            for account in accounts:
                session.expunge(account)
            return accounts

    def list_active(self) -> list[Account]:
        with self._session_factory() as session:
            accounts = list(
                session.scalars(
                    select(Account).where(Account.status == "active").order_by(Account.email)
                )
            )
            for account in accounts:
                session.expunge(account)
            return accounts

    def resolve(self, email: str | None) -> Account:
        """Pick the account to send from: the requested one, or the only active one.

        When nothing is active but a single account is on file it is still returned, so the
        caller can say "this account is revoked" instead of the misleading "nothing connected".
        """
        if email:
            return self.get(email)
        active = self.list_active()
        if len(active) == 1:
            return active[0]
        if not active:
            everything = self.list_all()
            if len(everything) == 1:
                return everything[0]
            if everything:
                statuses = sorted({a.email: a.status for a in everything}.items())
                raise AccountNotFound(
                    "no connected account is active; reconnect the account you want to use",
                    details={"accounts": dict(statuses)},
                )
            raise AccountNotFound(
                "no Gmail account is connected; run the OAuth connect flow first",
                details={"account": email},
            )
        raise InvalidRequest(
            "multiple Gmail accounts are connected; specify which one to use",
            details={"candidates": [a.email for a in active]},
        )

    def summaries(self) -> list[AccountSummary]:
        return [
            AccountSummary(
                email=a.email,
                account_type=a.account_type,
                status=a.status,
                scopes=list(a.scopes or []),
                created_at=a.created_at,
                next_send_at=a.next_send_at,
            )
            for a in self.list_all()
        ]

    # ---------------------------------------------------------------- internals

    def _require(self, session: Session, email: str) -> Account:
        normalized = email.strip().lower()
        account = session.scalar(select(Account).where(Account.email == normalized))
        if account is None:
            raise AccountNotFound("account not found", details={"account": email})
        return account

    def _apply_limit_defaults(self, account: Account) -> None:
        settings = self._settings
        account.daily_message_limit = settings.default_daily_message_limit
        account.daily_recipient_limit = settings.default_daily_recipient_limit
        account.soft_limit_ratio = settings.soft_limit_ratio
        account.send_interval_seconds = settings.default_send_interval_seconds
