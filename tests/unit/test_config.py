import base64
import pathlib

import pytest
from pydantic import ValidationError

from fmaiily.config import Settings

FAKE_KEY = base64.urlsafe_b64encode(b"0" * 32).decode()


def test_defaults_require_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FMAIILY_TOKEN_ENCRYPTION_KEY", raising=False)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_env_prefix_and_list_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FMAIILY_TOKEN_ENCRYPTION_KEY", FAKE_KEY)
    monkeypatch.setenv("FMAIILY_OAUTH_SCOPES", "a,b, c")
    s = Settings(_env_file=None)
    assert s.oauth_scopes == ["a", "b", "c"]
    assert s.environment == "dev"


def test_key_must_decode_to_32_bytes() -> None:
    with pytest.raises(ValidationError):
        Settings(
            token_encryption_key=base64.urlsafe_b64encode(b"short").decode(),
            _env_file=None,
        )


def test_key_accepts_base64_variants() -> None:
    raw = b"k" * 32
    for encoded in (
        base64.urlsafe_b64encode(raw).decode(),
        base64.b64encode(raw).decode(),
        base64.urlsafe_b64encode(raw).decode().rstrip("="),
    ):
        s = Settings(token_encryption_key=encoded, _env_file=None)
        assert len(s.encryption_key_bytes()) == 32


def test_oauth_configured_flag() -> None:
    s = Settings(
        token_encryption_key=FAKE_KEY,
        google_oauth_client_id="id",
        google_oauth_client_secret="sec",
        _env_file=None,
    )
    assert s.is_oauth_configured is True


def test_sqlite_path_extraction() -> None:
    s = Settings(
        token_encryption_key=FAKE_KEY,
        database_url="sqlite:////data/x.db",
        _env_file=None,
    )
    assert str(s.sqlite_path) == "/data/x.db"
    s2 = Settings(
        token_encryption_key=FAKE_KEY,
        database_url="sqlite:///./rel.db",
        _env_file=None,
    )
    assert s2.sqlite_path == pathlib.Path("./rel.db")
    s3 = Settings(
        token_encryption_key=FAKE_KEY,
        database_url="postgresql+psycopg://u@h/db",
        _env_file=None,
    )
    assert s3.sqlite_path is None
