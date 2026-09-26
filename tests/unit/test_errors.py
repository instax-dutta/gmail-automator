from fmaiily.errors import (
    AccountNotFound,
    GatewayError,
    QuotaExceeded,
    SendFailed,
    Unauthorized,
    to_envelope,
)


def test_envelope_shape() -> None:
    exc = QuotaExceeded("daily message soft limit reached", details={"resource": "messages"})
    assert to_envelope(exc) == {
        "error": {
            "code": "quota_exceeded",
            "message": "daily message soft limit reached",
            "details": {"resource": "messages"},
        }
    }


def test_error_codes_and_statuses_are_stable() -> None:
    assert (Unauthorized.code, Unauthorized.http_status) == ("unauthorized", 401)
    assert (AccountNotFound.code, AccountNotFound.http_status) == ("account_not_found", 404)
    assert (SendFailed.code, SendFailed.http_status) == ("send_failed", 502)


def test_all_errors_are_gateway_errors() -> None:
    for exc_type in (Unauthorized, AccountNotFound, QuotaExceeded, SendFailed):
        assert issubclass(exc_type, GatewayError)
