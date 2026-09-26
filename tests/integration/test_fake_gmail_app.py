from fastapi.testclient import TestClient

from tests.support.fake_gmail_app import DEFAULT_ACCOUNT, fake_gmail_app


def _client() -> TestClient:
    return TestClient(fake_gmail_app())


def test_token_endpoint_records_form_and_returns_refresh_token() -> None:
    with _client() as client:
        resp = client.post(
            "/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": "existing-refresh",
                "scope": "a b   c",
            },
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["access_token"] == "fake-access-token"
    assert body["refresh_token"] == "existing-refresh"
    assert body["scope"] == "a b c"


def test_userinfo_returns_default_account_and_records_auth_header() -> None:
    with _client() as client:
        resp = client.get("/v1/userinfo", headers={"authorization": "Bearer tok"})
    assert resp.json()["email"] == DEFAULT_ACCOUNT


def test_send_returns_message_and_thread_ids() -> None:
    with _client() as client:
        resp = client.post(
            "/gmail/v1/users/me/messages/send",
            json={"raw": "aGk="},
            headers={"authorization": "Bearer tok"},
        )
    assert resp.status_code == 200
    assert resp.json() == {"id": "fake-msg-1", "threadId": "fake-thread-1", "labelIds": ["SENT"]}


def test_scripted_error_status_is_returned_and_consumed() -> None:
    app = fake_gmail_app()
    app.state.behavior["send"] = {"status": 429, "times": 1}
    with TestClient(app) as client:
        first = client.post("/gmail/v1/users/me/messages/send", json={"raw": "aGk="})
        second = client.post("/gmail/v1/users/me/messages/send", json={"raw": "aGk="})
    assert first.status_code == 429
    assert first.json()["error"]["errors"][0]["reason"] == "rateLimitExceeded"
    assert second.status_code == 200  # scripted behavior is one-shot


def test_drafts_endpoint_is_available() -> None:
    with _client() as client:
        resp = client.post("/gmail/v1/users/me/drafts", json={"message": {"raw": "aGk="}})
    assert resp.json() == {"id": "draft-1", "message": {"id": "draft-msg-1"}}


def test_all_requests_are_recorded() -> None:
    app = fake_gmail_app()
    with TestClient(app) as client:
        client.post("/token", data={"grant_type": "refresh_token", "refresh_token": "r"})
        client.get("/v1/userinfo")
        client.post("/gmail/v1/users/me/messages/send", json={"raw": "aGk="})
    paths = [entry["path"] for entry in app.state.requests]
    assert paths == ["/token", "/v1/userinfo", "send"]
