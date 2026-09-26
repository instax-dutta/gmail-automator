"""The status page over HTTP (Phase 3, P5)."""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_status_page_is_served_over_http(client: TestClient, connected: str) -> None:
    response = client.get("/status")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "<!doctype html>" in response.text
    assert connected in response.text


def test_status_page_is_open_like_health(client: TestClient) -> None:
    assert client.get("/status").status_code == 200


def test_status_page_never_shows_a_token(client: TestClient, connected: str) -> None:
    body = client.get("/status").text
    assert "fake-refresh-token" not in body
    assert "fake-access-token" not in body
    assert "v1." not in body
