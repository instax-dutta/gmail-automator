from __future__ import annotations

import base64
from pathlib import Path
from typing import Literal

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

GMAIL_SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"
DEFAULT_SCOPES = [GMAIL_SEND_SCOPE, "openid", "email"]


def _b64d(raw: str) -> bytes:
    return base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="FMAIILY_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        # Env values reach `_split_csv` as raw strings (a,b,c) instead of being JSON-decoded
        # first, which is what operators expect in a .env file.
        enable_decoding=False,
    )

    environment: Literal["dev", "prod"] = "dev"
    database_url: str = "sqlite:///./data/fmaiily.db"
    token_encryption_key: SecretStr

    google_oauth_client_id: str | None = None
    google_oauth_client_secret: SecretStr | None = None
    oauth_redirect_uri: str = "http://localhost:8000/v1/oauth/google/callback"
    oauth_scopes: list[str] = DEFAULT_SCOPES
    oauth_token_uri: str = "https://oauth2.googleapis.com/token"
    oidc_userinfo_url: str = "https://openidconnect.googleapis.com/v1/userinfo"
    gmail_api_endpoint: str = "https://gmail.googleapis.com"

    default_daily_message_limit: int = 500
    default_daily_recipient_limit: int = 500
    soft_limit_ratio: float = 0.85
    default_send_interval_seconds: float = 2.0
    max_schedule_horizon_hours: float = 24.0
    max_recipients_per_message: int = 500
    max_body_bytes: int = 1_048_576

    worker_enabled: bool = True
    worker_poll_interval_seconds: float = 1.0
    worker_lease_seconds: int = 120
    worker_id: str | None = None
    max_attempts: int = 5
    backoff_base_seconds: float = 1.0
    backoff_max_seconds: float = 64.0
    queue_max_depth: int = 1000
    send_wait_timeout_seconds: float = 30.0
    poll_interval_seconds: float = 0.1
    payload_retention_hours: float = 24.0
    keep_sent_payloads: bool = False
    history_retention_days: int = 30

    auth_mode: Literal["api_key", "none"] = "api_key"
    bootstrap_admin_key: SecretStr | None = None

    attachments_enabled: bool = False
    attachment_max_bytes: int = 10_485_760
    attachment_allowed_dirs: list[str] = []

    log_level: str = "INFO"
    log_json: bool = True
    metrics_enabled: bool = True

    mcp_mount_path: str = "/mcp"
    host: str = "127.0.0.1"
    port: int = 8000
    request_timeout_seconds: float = 30.0

    @field_validator("oauth_scopes", "attachment_allowed_dirs", mode="before")
    @classmethod
    def _split_csv(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("token_encryption_key")
    @classmethod
    def _validate_key(cls, value: SecretStr) -> SecretStr:
        raw = value.get_secret_value()
        try:
            decoded = _b64d(raw)
        except Exception as exc:  # normalize any decode failure to a clear message
            raise ValueError("token_encryption_key must be base64 (urlsafe) encoded") from exc
        if len(decoded) != 32:
            raise ValueError("token_encryption_key must decode to exactly 32 bytes")
        return value

    def encryption_key_bytes(self) -> bytes:
        return _b64d(self.token_encryption_key.get_secret_value())

    @property
    def is_oauth_configured(self) -> bool:
        return bool(self.google_oauth_client_id and self.google_oauth_client_secret)

    @property
    def sqlite_path(self) -> Path | None:
        if not self.database_url.startswith("sqlite"):
            return None
        _, _, tail = self.database_url.partition(":///")
        return Path(tail) if tail else None

    @property
    def alembic_ini_path(self) -> Path:
        """Repository-root alembic.ini, whether running from a checkout or an installed wheel."""
        packaged = Path(__file__).resolve().parent / "alembic.ini"
        if packaged.is_file():
            return packaged
        return Path(__file__).resolve().parents[2] / "alembic.ini"
