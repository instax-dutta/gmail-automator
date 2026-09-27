"""Draft mode (PRD 9, Phase 3).

Creating a draft needs `gmail.compose` (or `gmail.modify`); an account connected with only
`gmail.send` cannot do it. The scope check happens in `DraftService`, before the API call, so the
agent gets `scope_missing` with the exact scope to add rather than an opaque 403 from Google.

These tests drive the real container, so they also prove `build_container` wires the service.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from gmail_automator.container import Container
from gmail_automator.drafts import COMPOSE_SCOPES, SEND_SCOPE, DraftService, has_compose_scope
from gmail_automator.errors import Forbidden, ScopeMissing
from gmail_automator.gmail.client import DraftResult, GoogleApiError
from gmail_automator.gmail.mime import OutgoingMessage
from gmail_automator.worker import Worker

NOW = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
COMPOSER = "composer@example.com"
SEND_ONLY = "send-only@example.com"


def _msg() -> OutgoingMessage:
    return OutgoingMessage(
        from_email="sender@example.com", to=("a@example.com",), subject="draft me", body="b"
    )


DEFAULT_SCOPES = ["https://www.googleapis.com/auth/gmail.send", "openid", "email"]


def _connect(container: Container, *, email: str) -> None:
    """Connect a real account through the fake provider so it holds genuine encrypted tokens.

    The provider grants whatever the authorization request asked for, so the granted scope set comes
    from the container's own `oauth_scopes` - exactly as Google behaves.
    """
    from tests.integration.conftest import FAKE_APP_MARKER

    assert container.oauth is not None
    FAKE_APP_MARKER.state.behavior["userinfo_email"] = email
    try:
        start = container.oauth.start()
        container.oauth.callback(code="code", state=start.state)
    finally:
        FAKE_APP_MARKER.state.behavior.pop("userinfo_email", None)


@pytest.fixture
def send_only_container(build_wired) -> Container:
    """A container whose connect flow grants only `gmail.send`."""
    return build_wired(oauth_scopes=[SEND_SCOPE, "openid", "email"])


@pytest.fixture
def compose_container(build_wired) -> Container:
    """A container whose connect flow also grants the compose scope."""
    return build_wired(oauth_scopes=[SEND_SCOPE, COMPOSE_SCOPES[0], "openid", "email"])


@pytest.fixture
def drafts(send_only_container: Container, compose_container: Container) -> DraftService:
    """The real DraftService, with one account of each scope kind connected for real."""
    assert send_only_container.drafts is not None
    assert compose_container.drafts is not None
    _connect(send_only_container, email=SEND_ONLY)
    _connect(compose_container, email=COMPOSER)
    return compose_container.drafts


def test_has_compose_scope_reads_the_account_scopes(drafts: DraftService) -> None:
    accounts = drafts._container.accounts
    assert has_compose_scope(accounts.get(SEND_ONLY)) is False
    assert has_compose_scope(accounts.get(COMPOSER)) is True


def test_a_send_only_account_is_refused_with_scope_missing(
    drafts: DraftService, fake_transport
) -> None:
    with pytest.raises(ScopeMissing) as excinfo:
        drafts.create_draft(account_email=SEND_ONLY, msg=_msg(), now=NOW)
    assert COMPOSE_SCOPES[0] in excinfo.value.message
    assert excinfo.value.details["required_scopes"] == list(COMPOSE_SCOPES)
    assert SEND_SCOPE in excinfo.value.details["granted_scopes"]
    assert COMPOSE_SCOPES[0] not in excinfo.value.details["granted_scopes"]
    assert fake_transport.draft_calls == []


def test_a_compose_account_gets_a_draft_id(drafts: DraftService) -> None:
    result = drafts.create_draft(account_email=COMPOSER, msg=_msg(), now=NOW)
    assert isinstance(result, DraftResult)
    assert result.draft_id == "draft-1"
    assert result.message_id == "draft-msg-1"


def test_the_draft_carries_the_encoded_message_and_the_right_account(
    drafts: DraftService, fake_transport
) -> None:
    import base64
    from email import message_from_bytes
    from email.policy import default as policy

    drafts.create_draft(account_email=COMPOSER, msg=_msg(), now=NOW)
    call = fake_transport.draft_calls[0]
    assert call["email"] == COMPOSER
    assert call["access_token"] == "fake-access-token"
    raw = base64.urlsafe_b64decode(call["raw_b64url"] + "===")
    assert message_from_bytes(raw, policy=policy)["Subject"] == "draft me"


def test_a_revoked_account_cannot_create_a_draft(drafts: DraftService, fake_transport) -> None:
    accounts = drafts._container.accounts
    accounts.revoke(COMPOSER)
    with pytest.raises(Forbidden):
        drafts.create_draft(account_email=COMPOSER, msg=_msg(), now=NOW)
    assert fake_transport.draft_calls == []


def test_draft_failures_are_normalized(drafts: DraftService, fake_transport) -> None:
    fake_transport.script_draft_error(
        GoogleApiError(status_code=400, reason="invalid", message="bad raw")
    )
    with pytest.raises(GoogleApiError) as excinfo:
        drafts.create_draft(account_email=COMPOSER, msg=_msg(), now=NOW)
    assert excinfo.value.status_code == 400
    assert excinfo.value.reason == "invalid"


def test_a_thread_id_is_forwarded(drafts: DraftService, fake_transport) -> None:
    drafts.create_draft(account_email=COMPOSER, msg=_msg(), now=NOW, thread_id="th-5")
    assert fake_transport.draft_calls[0]["thread_id"] == "th-5"


def test_creating_a_draft_never_queues_or_consumes_quota(drafts: DraftService) -> None:
    container = drafts._container
    assert container.queue is not None and container.quota is not None
    drafts.create_draft(account_email=COMPOSER, msg=_msg(), now=NOW)
    assert container.queue.depth() == 0
    account = container.accounts.get(COMPOSER)
    assert container.quota.snapshot(account, now=NOW).messages_remaining == 425


def test_drafting_leaves_the_send_worker_idle(drafts: DraftService) -> None:
    container = drafts._container
    drafts.create_draft(account_email=COMPOSER, msg=_msg(), now=NOW)
    worker = Worker(container, worker_id="w", rand=lambda: 0.0)
    assert worker.run_once(now=NOW).action == "idle"


def test_compose_scope_is_a_connect_time_choice(drafts: DraftService) -> None:
    """gmail-automator cannot widen a grant silently: the operator reconnects with the extra scope."""
    assert COMPOSE_SCOPES == (
        "https://www.googleapis.com/auth/gmail.modify",
        "https://www.googleapis.com/auth/gmail.compose",
    )
    account = drafts._container.accounts.get(COMPOSER)
    assert COMPOSE_SCOPES[0] in (account.scopes or [])


def test_an_expiring_token_is_refreshed_before_drafting(
    drafts: DraftService, fake_transport
) -> None:
    accounts = drafts._container.accounts
    accounts.expire_token(COMPOSER)
    drafts.create_draft(account_email=COMPOSER, msg=_msg(), now=NOW)
    assert accounts.get(COMPOSER).access_token_enc is not None
    assert fake_transport.draft_calls[0]["access_token"] == "fake-access-token"
