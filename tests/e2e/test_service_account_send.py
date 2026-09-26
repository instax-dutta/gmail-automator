"""A service-account account, from registration through a real send (Phase 3, P6)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from fmaiily.container import Container, build_container
from fmaiily.db import Base, create_db_engine
from fmaiily.errors import SendFailed
from fmaiily.gmail.mime import OutgoingMessage
from fmaiily.service_accounts import SERVICE_ACCOUNT_SCOPES, ServiceAccountConfig
from fmaiily.worker import Worker
from tests.support.fakes import FakeClock, FakeGmailTransport

NOW = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
SUBJECT = "agent@acme.co"
TOKEN_URI = "http://oauth.test/token"


@pytest.fixture(scope="module")
def rsa_key() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()


@pytest.fixture
def key_file(tmp_path, rsa_key: str) -> str:
    path = tmp_path / "sa.json"
    path.write_text(
        json.dumps(
            {
                "type": "service_account",
                "project_id": "acme-agents",
                "private_key_id": "key-1",
                "private_key": rsa_key,
                "client_email": "fmaiily@acme-agents.iam.gserviceaccount.com",
                "client_id": "1234567890",
                "token_uri": TOKEN_URI,
            }
        )
    )
    return str(path)


class _FakeRequest:
    def __init__(self, *, access_token: str = "sa-access-token", status: int = 200) -> None:
        self.access_token = access_token
        self.status = status
        self.calls: list[str] = []

    def __call__(self, url: str, method: str = "GET", body=None, headers=None, **kwargs: Any):
        self.calls.append(url)
        payload = json.dumps({"access_token": self.access_token, "expires_in": 3600}).encode()
        return self.status, {"content-type": "application/json"}, payload


@pytest.fixture
def sa_container(settings, seeded_engine, sleeper, fake_transport, key_file):
    tuned = settings.model_copy(
        update={
            "service_account_key_file": key_file,
            "service_account_subject": SUBJECT,
            "oauth_token_uri": TOKEN_URI,
        }
    )
    return build_container(
        tuned,
        engine=seeded_engine,
        transport=fake_transport,
        clock=FakeClock(),
        sleeper=sleeper,
        token_request=_FakeRequest(),
    )


def _register(container: Container) -> None:
    assert container.accounts is not None
    container.accounts.register_service_account(
        email=SUBJECT, scopes=list(SERVICE_ACCOUNT_SCOPES), now=NOW
    )


def test_the_container_loads_the_key(sa_container: Container) -> None:
    config = sa_container.service_account
    assert isinstance(config, ServiceAccountConfig)
    assert config.client_email == "fmaiily@acme-agents.iam.gserviceaccount.com"
    assert config.subject == SUBJECT


def test_registration_stores_no_token(sa_container: Container) -> None:
    _register(sa_container)
    account = sa_container.accounts.get(SUBJECT)
    assert account.auth_type == "service_account"
    assert account.account_type == "workspace"
    assert account.access_token_enc is None
    assert account.refresh_token_enc is None
    assert account.scopes == list(SERVICE_ACCOUNT_SCOPES)


def test_registration_applies_the_same_quota_policy(sa_container: Container) -> None:
    _register(sa_container)
    account = sa_container.accounts.get(SUBJECT)
    assert account.daily_message_limit == 500
    assert account.soft_limit_ratio == pytest.approx(0.85)
    assert account.send_interval_seconds == pytest.approx(2.0)
    assert sa_container.quota.snapshot(account, now=NOW).messages_remaining == 425


def test_a_token_is_minted_and_never_persisted(sa_container: Container) -> None:
    _register(sa_container)
    token = sa_container.tokens.access_token(SUBJECT, now=NOW)
    assert token.token == "sa-access-token"
    assert token.refreshed is True
    assert token.expiry == NOW + timedelta(hours=1)
    assert sa_container.accounts.get(SUBJECT).access_token_enc is None


def test_a_service_account_send_reaches_gmail(sa_container: Container, fake_transport) -> None:
    _register(sa_container)
    outcome = sa_container.sender.send(
        account_email=SUBJECT,
        msg=OutgoingMessage(
            from_email=SUBJECT, to=("a@example.com",), subject="from a service account", body="b"
        ),
        source="api",
        wait=False,
        now=NOW,
    )
    assert outcome.status == "queued"
    worker = Worker(sa_container, worker_id="w", rand=lambda: 0.0)
    assert worker.run_once(now=NOW).action == "sent"
    assert fake_transport.calls[0]["email"] == SUBJECT
    assert fake_transport.calls[0]["access_token"] == "sa-access-token"


def test_the_subject_must_match_the_configured_impersonation(sa_container: Container) -> None:
    _register(sa_container)
    sa_container.accounts.register_service_account(
        email="other@acme.co", scopes=list(SERVICE_ACCOUNT_SCOPES), now=NOW
    )
    with pytest.raises(SendFailed) as excinfo:
        sa_container.tokens.access_token("other@acme.co", now=NOW)
    assert "impersonates" in excinfo.value.message


def test_a_rejected_assertion_fails_the_job_not_the_worker(
    settings, seeded_engine, sleeper, fake_transport, key_file
) -> None:
    sa_container = build_container(
        settings.model_copy(
            update={
                "service_account_key_file": key_file,
                "service_account_subject": SUBJECT,
                "oauth_token_uri": TOKEN_URI,
            }
        ),
        engine=seeded_engine,
        transport=fake_transport,
        clock=FakeClock(),
        sleeper=sleeper,
        token_request=_FakeRequest(status=400),
    )
    _register(sa_container)
    outcome = sa_container.sender.send(
        account_email=SUBJECT,
        msg=OutgoingMessage(from_email=SUBJECT, to=("a@example.com",), subject="s", body="b"),
        source="api",
        wait=False,
        now=NOW,
    )
    result = Worker(sa_container, worker_id="w", rand=lambda: 0.0).run_once(now=NOW)
    assert result.action == "failed"
    assert result.error_code == "send_failed"
    assert sa_container.queue.get(outcome.job_id).status == "failed"
    assert fake_transport.calls == []


def test_a_revoked_service_account_is_refused(sa_container: Container) -> None:
    _register(sa_container)
    sa_container.accounts.revoke(SUBJECT)
    with pytest.raises(SendFailed):
        sa_container.tokens.access_token(SUBJECT, now=NOW)


def test_no_configuration_means_no_service_account(settings, seeded_engine) -> None:
    container = build_container(settings, engine=seeded_engine, transport=FakeGmailTransport())
    assert container.service_account is None
    assert settings.is_service_account_configured is False


def test_a_half_configured_service_account_is_ignored(settings, seeded_engine, key_file) -> None:
    """Key without subject, or subject without key, is not a usable configuration."""
    for update in (
        {"service_account_key_file": key_file, "service_account_subject": None},
        {"service_account_key_file": None, "service_account_subject": SUBJECT},
    ):
        tuned = settings.model_copy(update=update)
        assert tuned.is_service_account_configured is False
        assert build_container(tuned, engine=seeded_engine).service_account is None


def test_a_bad_key_path_fails_loudly_at_startup(settings, seeded_engine) -> None:
    from fmaiily.errors import InvalidRequest

    tuned = settings.model_copy(
        update={
            "service_account_key_file": "/nonexistent/sa.json",
            "service_account_subject": SUBJECT,
        }
    )
    with pytest.raises(InvalidRequest):
        build_container(tuned, engine=seeded_engine)
    assert Base.metadata is not None
    assert create_db_engine is not None
