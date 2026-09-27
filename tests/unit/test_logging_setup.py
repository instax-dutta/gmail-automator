import json
import logging

import pytest
import structlog

from gmail_automator.logging_setup import REDACTED, configure_logging, get_logger, redact_processor

SECRET_FIELDS = (
    "access_token",
    "refresh_token",
    "api_key",
    "password",
    "authorization",
    "client_secret",
    "authorization_code",
    "otp",
)


def _emitted(caplog: pytest.LogCaptureFixture) -> list[dict]:
    """Structured events as they reached the logging system (pre-render)."""
    events = []
    for record in caplog.records:
        if isinstance(record.msg, dict):
            event = dict(record.msg)
            event.pop("_record", None)
            event.pop("_from_structlog", None)
            events.append(event)
    return events


def test_structured_fields_are_passed_through(caplog: pytest.LogCaptureFixture) -> None:
    configure_logging(level="INFO", json_output=True)
    with caplog.at_level(logging.INFO):
        get_logger("gmail_automator.test").info(
            "send_attempt", account="me@example.com", recipients=2
        )
    event = _emitted(caplog)[-1]
    assert event["event"] == "send_attempt"
    assert event["account"] == "me@example.com"
    assert event["recipients"] == 2
    assert event["level"] == "info"
    assert "timestamp" in event


def test_secrets_are_redacted_in_emitted_events(caplog: pytest.LogCaptureFixture) -> None:
    configure_logging(level="INFO", json_output=True)
    with caplog.at_level(logging.INFO):
        get_logger("gmail_automator.test").info(
            "token_stuff",
            access_token="ya29.abcdef",
            refresh_token="1//refresh",
            api_key="fmg_secret",
            password="hunter2",
            authorization="Bearer abc",
            client_secret="GOCSPX-xyz",
            authorization_code="4/abc",
            otp="123456",
        )
    event = _emitted(caplog)[-1]
    for field in SECRET_FIELDS:
        assert event[field] == REDACTED
    assert "ya29.abcdef" not in json.dumps(event)
    assert "hunter2" not in json.dumps(event)


def test_plain_stdlib_records_do_not_render_extra_secrets(
    capfd: pytest.CaptureFixture[str],
) -> None:
    configure_logging(level="INFO", json_output=True)
    logging.getLogger("plain").info("raw", extra={"api_key": "fmg_leak"})
    assert "fmg_leak" not in capfd.readouterr().out


def test_redaction_recurses_into_nested_structures() -> None:
    event = redact_processor(
        None,  # type: ignore[arg-type]
        "info",
        {
            "event": "x",
            "account": {"email": "me@x.com", "refresh_token": "1//leak"},
            "history": [{"api_key": "fmg_leak"}, {"authorization_code": "4/abc"}],
            "tuple": ("secret", "fine"),
        },
    )
    assert event["account"] == {"email": "me@x.com", "refresh_token": REDACTED}
    assert event["history"] == [{"api_key": REDACTED}, {"authorization_code": REDACTED}]
    # a bare string inside a sequence has no key, so key-based redaction leaves it alone
    assert event["tuple"] == ("secret", "fine")


def test_redaction_is_key_based_so_bodies_survive() -> None:
    body = "your verification code is 123456, ref code 4/abc"
    event = redact_processor(None, "info", {"event": "x", "body": body})  # type: ignore[arg-type]
    assert event["body"] == body


def test_operational_code_fields_survive_redaction() -> None:
    """`error_code` is what an operator reads when a send is refused; it must not be redacted."""
    event = redact_processor(
        None,  # type: ignore[arg-type]
        "info",
        {"event": "send_failed", "error_code": "quota_exceeded", "status_code": 429},
    )
    assert event["error_code"] == "quota_exceeded"
    assert event["status_code"] == 429


def test_redaction_is_case_insensitive_and_normalizes_separators() -> None:
    event = redact_processor(
        None,  # type: ignore[arg-type]
        "info",
        {"event": "x", "X-Api-Key": "k", "user_token": "t", "monkey": "not-a-key"},
    )
    assert event["X-Api-Key"] == REDACTED
    assert event["user_token"] == REDACTED
    assert event["monkey"] == "not-a-key"  # "key" appears but not as a denylisted fragment


def test_redaction_ignores_non_string_values() -> None:
    event = redact_processor(
        None,  # type: ignore[arg-type]
        "info",
        {"event": "x", "api_key": None, "recipients": 3},
    )
    assert event["api_key"] is None
    assert event["recipients"] == 3


def test_console_renderer_for_non_json(capfd: pytest.CaptureFixture[str]) -> None:
    configure_logging(level="INFO", json_output=False)
    get_logger("gmail_automator.test").info("hello", account="me@example.com")
    out = capfd.readouterr().out
    assert "hello" in out
    assert "me@example.com" in out


def test_configure_logging_replaces_only_its_own_handler() -> None:
    def ours() -> list[logging.Handler]:
        return [
            h
            for h in logging.getLogger().handlers
            if isinstance(h.formatter, structlog.stdlib.ProcessorFormatter)
        ]

    configure_logging(level="INFO", json_output=True)
    assert len(ours()) == 1
    configure_logging(level="INFO", json_output=True)
    assert len(ours()) == 1  # replaced, not accumulated


def test_rendered_json_line_is_parseable_and_redacted() -> None:
    configure_logging(level="INFO", json_output=True)
    event = redact_processor(
        None,  # type: ignore[arg-type]
        "info",
        {"event": "evt", "n": 1, "access_token": "secret"},
    )
    rendered = structlog.processors.JSONRenderer()(None, "info", event)  # type: ignore[arg-type]
    assert json.loads(rendered) == {"event": "evt", "n": 1, "access_token": REDACTED}
