from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from fmaiily.errors import GatewayError, to_envelope
from fmaiily.logging_setup import get_logger

_log = get_logger("fmaiily.rest")

#: Starlette status codes that already carry an agent-facing meaning.
_STATUS_CODES: dict[int, str] = {
    400: "invalid_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "duplicate_request",
    413: "attachment_too_large",
    422: "invalid_request",
    429: "quota_exceeded",
    503: "queue_full",
}


def error_response(
    code: str, message: str, status: int, details: dict[str, Any] | None = None
) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": {"code": code, "message": message, "details": details or {}}},
    )


def register_exception_handlers(app: FastAPI) -> None:
    """Every failure leaves the API as the one documented envelope shape."""

    @app.exception_handler(GatewayError)
    async def _gateway_error(request: Request, exc: GatewayError) -> JSONResponse:
        if exc.http_status >= 500:
            _log.error(
                "request_failed",
                path=request.url.path,
                code=exc.code,
                error_type=type(exc).__name__,
            )
        else:
            _log.info(
                "request_rejected",
                path=request.url.path,
                code=exc.code,
                status=exc.http_status,
            )
        return JSONResponse(status_code=exc.http_status, content=to_envelope(exc))

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        # A malformed body is a 400; a bad query or path parameter keeps FastAPI's 422, which
        # agents already know how to read. Both use the same envelope and the same code.
        locations = [item.get("loc", ()) for item in exc.errors()]
        from_query_or_path = any(loc and loc[0] in ("query", "path") for loc in locations)
        return JSONResponse(
            status_code=422 if from_query_or_path else 400,
            content={
                "error": {
                    "code": "invalid_request",
                    "message": "request body or parameters failed validation",
                    "details": {"errors": _safe_errors(exc)},
                }
            },
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = _STATUS_CODES.get(exc.status_code, "http_error")
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": code, "message": str(exc.detail), "details": {}}},
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        # The message is deliberately generic: an unexpected exception may carry a SQL string
        # or a token in its text, and neither belongs in an API response.
        _log.exception("unhandled_error", path=request.url.path, error_type=type(exc).__name__)
        return error_response("internal_error", "the gateway failed to handle this request", 500)


def _safe_errors(exc: RequestValidationError) -> list[dict[str, Any]]:
    cleaned: list[dict[str, Any]] = []
    for item in exc.errors():
        entry: dict[str, Any] = {
            "location": ".".join(str(part) for part in item.get("loc", ())),
            "message": str(item.get("msg", "invalid value")),
            "type": str(item.get("type", "invalid")),
        }
        if "ctx" in item:
            entry["context"] = {k: str(v) for k, v in item["ctx"].items()}
        cleaned.append(entry)
    return cleaned


logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

__all__ = ["error_response", "register_exception_handlers"]
