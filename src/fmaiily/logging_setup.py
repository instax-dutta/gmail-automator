from __future__ import annotations

import logging
import sys
from typing import Any

import structlog
import structlog.stdlib

REDACTED = "[redacted]"

#: Any event key containing one of these fragments has its string/bytes value replaced with
#: ``REDACTED``. Matching is case-insensitive, and ``-``/``.``/spaces in the key are normalized to
#: ``_`` first, so ``refresh_token``, ``X-Api-Key``, and ``client.secret`` are all covered.
SECRET_KEY_FRAGMENTS: tuple[str, ...] = (
    "token",
    "secret",
    "authorization",
    "api_key",
    "apikey",
    "password",
    "passwd",
    "code",
    "credential",
    "cookie",
)


def _is_secret_key(key: str) -> bool:
    lowered = key.lower().replace("-", "_").replace(".", "_").replace(" ", "_")
    return any(fragment in lowered for fragment in SECRET_KEY_FRAGMENTS)


def _redact_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            k: (
                REDACTED
                if _is_secret_key(str(k)) and isinstance(v, (str, bytes))
                else _redact_value(v)
            )
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_value(item) for item in value)
    return value


def redact_processor(logger: Any, method_name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    """structlog processor that strips secret-looking values before rendering.

    Redaction is *key*-based, never value-based: a message body or an email address that happens
    to contain the word "code" must survive. Application code therefore passes secrets as
    keyword arguments (`access_token=...`), never interpolated into the event string. Nested
    dicts and sequences are walked so a secret cannot hide one level down.
    """
    return {
        key: (
            REDACTED
            if _is_secret_key(str(key)) and isinstance(value, (str, bytes))
            else _redact_value(value)
        )
        for key, value in event_dict.items()
    }


def _shared_processors() -> list[Any]:
    return [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        redact_processor,
    ]


_handler: logging.Handler | None = None


def configure_logging(
    *,
    level: str = "INFO",
    json_output: bool = True,
    stream: Any | None = None,
) -> None:
    """(Re)configure stdlib logging and structlog. Safe to call more than once.

    Both the structlog and the plain-stdlib paths funnel through the same processor chain, so a
    secret is redacted no matter which one emitted the record. Only the handler this function
    installed is replaced; handlers owned by the host application are left alone.

    `stream` matters for the CLI: anything it prints to stdout may be parsed as JSON, so logs
    there have to go to stderr instead.
    """
    global _handler
    shared = _shared_processors()
    renderer: Any = (
        structlog.processors.JSONRenderer()
        if json_output
        else structlog.dev.ConsoleRenderer(colors=False)
    )

    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=False,
    )

    root = logging.getLogger()
    if _handler is not None:
        root.removeHandler(_handler)
    _handler = logging.StreamHandler(stream or sys.stdout)
    _handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=shared,
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                renderer,
            ],
        )
    )
    root.addHandler(_handler)
    root.setLevel(level.upper())

    for noisy in ("googleapiclient", "google.auth", "urllib3", "httpx", "httpcore", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> Any:
    return structlog.stdlib.get_logger(name)
