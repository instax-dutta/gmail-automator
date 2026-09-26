from __future__ import annotations

from typing import Any, ClassVar


class GatewayError(Exception):
    """Base class for every error the gateway reports to a client or an agent.

    ``code`` is the stable, agent-facing identifier; ``http_status`` is the REST mapping.
    Both are class attributes so error handling never depends on string matching.
    """

    code: ClassVar[str] = "internal_error"
    http_status: ClassVar[int] = 500

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details: dict[str, Any] = details or {}


class InvalidRequest(GatewayError):
    code, http_status = "invalid_request", 400


class Unauthorized(GatewayError):
    code, http_status = "unauthorized", 401


class Forbidden(GatewayError):
    code, http_status = "forbidden", 403


class AccountNotFound(GatewayError):
    code, http_status = "account_not_found", 404


class ScopeMissing(GatewayError):
    code, http_status = "scope_missing", 403


class QuotaExceeded(GatewayError):
    code, http_status = "quota_exceeded", 429


class QueueFull(GatewayError):
    code, http_status = "queue_full", 503


class DuplicateRequest(GatewayError):
    code, http_status = "duplicate_request", 409


class SendFailed(GatewayError):
    code, http_status = "send_failed", 502


class DailySendQuotaExceeded(GatewayError):
    code, http_status = "daily_send_quota_exceeded", 502


class AttachmentTooLarge(GatewayError):
    code, http_status = "attachment_too_large", 413


class AttachmentPathNotAllowed(GatewayError):
    code, http_status = "attachment_path_not_allowed", 400


def to_envelope(exc: GatewayError) -> dict[str, Any]:
    return {"error": {"code": exc.code, "message": exc.message, "details": exc.details}}
