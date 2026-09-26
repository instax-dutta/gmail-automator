"""Shared fixtures for the HTTP surface tests: a real app over a real (temporary) SQLite file."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from fmaiily.container import Container, build_container
from fmaiily.rest.app import create_app
from tests.support.fake_gmail_app import fake_gmail_app
from tests.support.sync_asgi import sync_asgi_client

FAKE_APP = fake_gmail_app()


@pytest.fixture
def http_settings(settings):
    return settings.model_copy(
        update={
            "google_oauth_client_id": "cid",
            "google_oauth_client_secret": SecretStr("csecret"),
            "oauth_authorization_uri": "http://oauth.test/authorize",
            "oauth_token_uri": "http://oauth.test/token",
            "oidc_userinfo_url": "http://oauth.test/v1/userinfo",
            "auth_mode": "none",
        }
    )


@pytest.fixture
def build_http_container(http_settings, seeded_engine, fake_clock, sleeper, fake_transport):
    """Factory so a test can vary settings *before* the services capture them."""

    def _build(**overrides) -> Container:
        tuned = http_settings.model_copy(update=overrides) if overrides else http_settings
        return build_container(
            tuned,
            engine=seeded_engine,
            transport=fake_transport,
            clock=fake_clock,
            sleeper=sleeper,
            http=sync_asgi_client(FAKE_APP, base_url="http://oauth.test"),
        )

    return _build


@pytest.fixture
def http_container(http_settings, seeded_engine, fake_clock, sleeper, fake_transport) -> Container:
    return build_container(
        http_settings,
        engine=seeded_engine,
        transport=fake_transport,
        clock=fake_clock,
        sleeper=sleeper,
        http=sync_asgi_client(FAKE_APP, base_url="http://oauth.test"),
    )


@pytest.fixture
def client(http_container: Container) -> Iterator[TestClient]:
    app = create_app(http_container, settings=http_container.settings, start_worker=False)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def connected(http_container: Container) -> str:
    """Complete a real OAuth connect against the fake Google app."""
    start = http_container.oauth.start()
    return http_container.oauth.callback(code="code", state=start.state).email


@pytest.fixture
def key_client(http_container: Container) -> Iterator[tuple[TestClient, Any]]:
    """An app in `auth_mode=api_key`, plus a factory for issuing keys against it."""
    from fmaiily.api_keys import ApiKeyService

    container = http_container
    container.settings = container.settings.model_copy(
        update={
            "auth_mode": "api_key",
            "bootstrap_admin_key": SecretStr("fmg_bootstrap_secret_value"),
        }
    )

    def issue(**kwargs: Any) -> str:
        _, full_key = ApiKeyService(container=container).create(
            name=kwargs.pop("name", "agent"), **kwargs
        )
        return full_key

    app = create_app(container, settings=container.settings, start_worker=False)
    with TestClient(app) as test_client:
        yield test_client, issue


@pytest.fixture
def authed_client(key_client) -> tuple[TestClient, str]:
    test_client, issue = key_client
    return test_client, issue(name="agent", scopes=("send", "read"))


@pytest.fixture(autouse=True)
def _reset_fake_app() -> Iterator[None]:
    FAKE_APP.state.requests.clear()
    FAKE_APP.state.behavior.clear()
    yield
    FAKE_APP.state.requests.clear()
    FAKE_APP.state.behavior.clear()
