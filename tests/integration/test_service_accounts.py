"""Service accounts and domain-wide delegation (Phase 3, P6).

A Workspace admin can authorize a service account to impersonate a user. Fmaiily then obtains access
tokens by signed JWT assertion instead of an interactive consent flow, so an unattended deployment
can send as a Workspace mailbox with no refresh token stored at all.

The security boundary is unchanged: only `gmail.send` is requested, tokens are encrypted at rest,
and every quota rule still applies.
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from fmaiily.errors import InvalidRequest, SendFailed
from fmaiily.service_accounts import (
    SERVICE_ACCOUNT_SCOPES,
    ServiceAccountConfig,
    load_service_account_key,
    mint_access_token,
    parse_service_account_json,
)

NOW = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
TOKEN_URI = "https://oauth2.googleapis.com/token"
SUBJECT = "agent@acme.co"


@pytest.fixture(scope="module")
def rsa_key() -> dict[str, str]:
    """A throwaway 2048-bit key, so the JWT assertion path is genuinely exercised."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    return {"private_key": private_pem}


@pytest.fixture
def key_json(rsa_key: dict[str, str]) -> dict[str, Any]:
    return {
        "type": "service_account",
        "project_id": "acme-agents",
        "private_key_id": "key-1",
        "private_key": rsa_key["private_key"],
        "client_email": "fmaiily@acme-agents.iam.gserviceaccount.com",
        "client_id": "1234567890",
        "token_uri": TOKEN_URI,
    }


@pytest.fixture
def issued() -> list[dict[str, Any]]:
    return []


class _FakeRequest:
    """A `google.auth.transport.Request` stand-in that records the assertion it was given."""

    def __init__(self, issued: list[dict[str, Any]], *, access_token: str = "sa-token") -> None:
        self._issued = issued
        self._access_token = access_token

    def __call__(
        self,
        url: str,
        method: str = "GET",
        body: str | bytes | None = None,
        headers: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> tuple[int, dict[str, str], bytes]:
        from urllib.parse import parse_qs

        raw = body.decode() if isinstance(body, bytes) else (body or "")
        parsed = {k: v[0] for k, v in parse_qs(raw).items()}
        self._issued.append(
            {"url": url, "method": method, "body": parsed, "headers": headers or {}}
        )
        response = json.dumps({"access_token": self._access_token, "expires_in": 3600}).encode()
        return 200, {"content-type": "application/json"}, response


# ------------------------------------------------------------------- parsing


def test_parse_service_account_json_reads_the_fields(key_json) -> None:
    config = parse_service_account_json(json.dumps(key_json), subject=SUBJECT)
    assert config.client_email == key_json["client_email"]
    assert config.subject == SUBJECT
    assert config.token_uri == TOKEN_URI
    assert config.project_id == "acme-agents"
    assert config.scopes == SERVICE_ACCOUNT_SCOPES


def test_parse_rejects_a_non_service_account_key(key_json) -> None:
    key_json["type"] = "authorized_user"
    with pytest.raises(InvalidRequest) as excinfo:
        parse_service_account_json(json.dumps(key_json), subject=SUBJECT)
    assert "service_account" in excinfo.value.message


def test_parse_requires_a_subject(key_json) -> None:
    """Without an impersonated user there is nothing to send as, and no domain-wide delegation."""
    with pytest.raises(InvalidRequest) as excinfo:
        parse_service_account_json(json.dumps(key_json), subject="")
    assert "subject" in excinfo.value.message.lower()


def test_parse_rejects_malformed_json() -> None:
    with pytest.raises(InvalidRequest):
        parse_service_account_json("{not json", subject=SUBJECT)


def test_parse_requires_the_mandatory_fields(key_json) -> None:
    del key_json["private_key"]
    with pytest.raises(InvalidRequest) as excinfo:
        parse_service_account_json(json.dumps(key_json), subject=SUBJECT)
    assert "private_key" in excinfo.value.message


def test_load_from_a_path(tmp_path, key_json) -> None:
    path = tmp_path / "sa.json"
    path.write_text(json.dumps(key_json))
    assert load_service_account_key(path, subject=SUBJECT).client_email == key_json["client_email"]


def test_load_rejects_a_missing_path(tmp_path) -> None:
    with pytest.raises(InvalidRequest):
        load_service_account_key(tmp_path / "absent.json", subject=SUBJECT)


# -------------------------------------------------------------------- minting


def test_mint_posts_a_signed_assertion(key_json, issued) -> None:
    config = ServiceAccountConfig.from_key(key_json, subject=SUBJECT)
    token, expiry = mint_access_token(
        config, request=_FakeRequest(issued), now=NOW, scopes=[SERVICE_ACCOUNT_SCOPES[0]]
    )
    assert token == "sa-token"
    assert expiry == NOW + timedelta(seconds=3600)
    assert len(issued) == 1
    call = issued[0]
    assert call["url"] == TOKEN_URI
    assert call["method"] == "POST"
    assert call["body"]["grant_type"] == "urn:ietf:params:oauth:grant-type:jwt-bearer"
    assert "assertion" in call["body"]


def test_the_assertion_claims_domain_wide_delegation(key_json, issued) -> None:
    import jwt  # google-auth ships PyJWT; decode without verifying to inspect the claims

    config = ServiceAccountConfig.from_key(key_json, subject=SUBJECT)
    mint_access_token(config, request=_FakeRequest(issued), now=NOW)
    assertion = issued[0]["body"]["assertion"]
    claims = jwt.decode(assertion, options={"verify_signature": False})
    assert claims["iss"] == key_json["client_email"]
    assert claims["sub"] == SUBJECT
    assert claims["aud"] == TOKEN_URI
    assert "gmail.send" in claims["scope"]
    assert claims["exp"] > claims["iat"]


def test_a_rejected_assertion_is_a_clean_error(key_json) -> None:
    def _failing_request(*args: Any, **kwargs: Any):
        return 400, {"content-type": "application/json"}, b'{"error":"unauthorized_client"}'

    config = ServiceAccountConfig.from_key(key_json, subject=SUBJECT)
    with pytest.raises(SendFailed) as excinfo:
        mint_access_token(config, request=_failing_request, now=NOW)
    assert "assertion" in excinfo.value.message.lower()


def test_no_refresh_token_is_ever_stored(key_json, issued) -> None:
    """A service account mints a token on demand; there is no long-lived secret to persist."""
    config = ServiceAccountConfig.from_key(key_json, subject=SUBJECT)
    mint_access_token(config, request=_FakeRequest(issued), now=NOW)
    assert issued[0]["body"].get("refresh_token") is None


def test_the_private_key_never_leaves_the_config(key_json) -> None:
    """`redacted()` is what logging and CLI output use, so it must be safe to print anywhere."""
    config = ServiceAccountConfig.from_key(key_json, subject=SUBJECT)
    redacted = config.redacted()
    assert "private_key" not in redacted
    assert redacted["client_email"] == key_json["client_email"]
    assert "PRIVATE KEY" not in json.dumps(redacted)


def test_base64_helper_stays_available() -> None:
    assert base64.urlsafe_b64encode(b"x").decode().strip("=") == "eA"
