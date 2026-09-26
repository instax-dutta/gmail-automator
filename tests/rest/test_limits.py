from datetime import timedelta

import pytest

from fmaiily.errors import QuotaExceeded
from fmaiily.rate_limit import KeyRateLimiter
from tests.support.fakes import FakeClock

# ------------------------------------------------------------- limiter alone


def test_no_limit_means_unlimited() -> None:
    limiter = KeyRateLimiter(clock=FakeClock())
    for _ in range(1000):
        limiter.check(1, None)
    assert limiter.used(1) == 0  # nothing is counted when there is nothing to enforce


def test_requests_within_the_limit_pass() -> None:
    limiter = KeyRateLimiter(clock=FakeClock())
    for _ in range(3):
        limiter.check(1, 3)
    assert limiter.used(1) == 3


def test_the_request_over_the_limit_is_refused() -> None:
    limiter = KeyRateLimiter(clock=FakeClock())
    for _ in range(3):
        limiter.check(1, 3)
    with pytest.raises(QuotaExceeded) as excinfo:
        limiter.check(1, 3)
    assert excinfo.value.details["resource"] == "api_key_rate"
    assert excinfo.value.details["limit_per_minute"] == 3
    assert excinfo.value.details["retry_after_seconds"] > 0


def test_the_window_resets_after_a_minute() -> None:
    clock = FakeClock()
    limiter = KeyRateLimiter(clock=clock)
    for _ in range(2):
        limiter.check(1, 2)
    with pytest.raises(QuotaExceeded):
        limiter.check(1, 2)
    clock.advance(timedelta(seconds=61))
    limiter.check(1, 2)
    assert limiter.used(1) == 1


def test_keys_have_independent_budgets() -> None:
    limiter = KeyRateLimiter(clock=FakeClock())
    limiter.check(1, 1)
    with pytest.raises(QuotaExceeded):
        limiter.check(1, 1)
    limiter.check(2, 1)  # a different key is unaffected


def test_a_refused_request_does_not_consume_budget() -> None:
    clock = FakeClock()
    limiter = KeyRateLimiter(clock=clock)
    limiter.check(1, 1)
    with pytest.raises(QuotaExceeded):
        limiter.check(1, 1)
    clock.advance(timedelta(seconds=61))
    limiter.check(1, 1)
    assert limiter.used(1) == 1


def test_reset_clears_one_key_or_all_of_them() -> None:
    limiter = KeyRateLimiter(clock=FakeClock())
    limiter.check(1, 1)
    limiter.check(2, 1)
    limiter.reset(1)
    assert limiter.used(1) == 0
    assert limiter.used(2) == 1
    limiter.reset()
    assert limiter.used(2) == 0


# ----------------------------------------------------------------- over HTTP


def test_api_route_enforces_the_key_limit(key_client, connected) -> None:
    test_client, issue = key_client
    key = issue(name="limited", scopes=("send", "read"), rate_limit_per_minute=2)
    headers = {"authorization": f"Bearer {key}"}

    assert test_client.get("/v1/quota", headers=headers).status_code == 200
    assert test_client.get("/v1/quota", headers=headers).status_code == 200
    refused = test_client.get("/v1/quota", headers=headers)
    assert refused.status_code == 429
    assert refused.json()["error"]["code"] == "quota_exceeded"
    assert refused.json()["error"]["details"]["resource"] == "api_key_rate"


def test_a_send_over_the_key_limit_is_refused(key_client, connected) -> None:
    test_client, issue = key_client
    key = issue(name="one-shot", scopes=("send", "read"), rate_limit_per_minute=1)
    headers = {"authorization": f"Bearer {key}"}
    body = {"to": ["a@example.com"], "subject": "s", "body": "b", "wait": False}
    assert test_client.post("/v1/send", json=body, headers=headers).status_code == 200
    refused = test_client.post("/v1/send", json=body, headers=headers)
    assert refused.status_code == 429
    assert refused.json()["error"]["details"]["resource"] == "api_key_rate"


def test_an_unlimited_key_is_never_refused(key_client, connected) -> None:
    test_client, issue = key_client
    headers = {"authorization": f"Bearer {issue(name='unlimited', scopes=('read',))}"}
    for _ in range(20):
        assert test_client.get("/v1/quota", headers=headers).status_code == 200


def test_read_only_key_cannot_send(key_client, connected) -> None:
    test_client, issue = key_client
    response = test_client.post(
        "/v1/send",
        json={"to": ["a@example.com"], "subject": "s", "body": "b", "wait": False},
        headers={"authorization": f"Bearer {issue(name='reader', scopes=('read',))}"},
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"


def test_read_only_key_cannot_read_accounts_detail(key_client, connected) -> None:
    test_client, issue = key_client
    response = test_client.get(
        f"/v1/accounts/{connected}",
        headers={"authorization": f"Bearer {issue(name='send-only', scopes=('send',))}"},
    )
    assert response.status_code == 403


def test_send_only_key_cannot_disconnect_an_account(key_client, connected) -> None:
    test_client, issue = key_client
    response = test_client.request(
        "DELETE",
        f"/v1/accounts/{connected}",
        headers={"authorization": f"Bearer {issue(name='sender', scopes=('send',))}"},
    )
    assert response.status_code == 403
    assert response.json()["error"]["details"]["required_scope"] == "admin"
    assert test_client.get("/v1/accounts", headers={"authorization": "Bearer "}).status_code == 401


# ------------------------------------------------------------------- metrics


def test_metrics_endpoint_is_served(client) -> None:
    response = client.get("/metrics")
    assert response.status_code == 200
    assert "text/plain" in response.headers["content-type"]
    assert "fmaiily_sends_total" in response.text


def test_metrics_count_requests_by_route_template(client, connected) -> None:
    client.get("/v1/quota")
    client.get("/v1/accounts")
    text = client.get("/metrics").text
    assert 'fmaiily_http_requests_total{method="GET",path="/v1/quota",status="200"}' in text
    assert 'fmaiily_http_requests_total{method="GET",path="/v1/accounts",status="200"}' in text


def test_metrics_never_label_a_concrete_account_or_job_id(client, connected) -> None:
    job = client.post(
        "/v1/send",
        json={"to": ["a@example.com"], "subject": "s", "body": "b", "wait": False},
    ).json()
    client.get(f"/v1/jobs/{job['job_id']}")
    text = client.get("/metrics").text
    assert f"/v1/jobs/{job['job_id']}" not in text
    assert 'path="/v1/jobs/{job_id}"' in text


def test_metrics_record_a_refused_request(client) -> None:
    client.get("/v1/quota/nobody@example.com")
    text = client.get("/metrics").text
    assert (
        'fmaiily_http_requests_total{method="GET",path="/v1/quota/{account}",status="404"}' in text
    )
