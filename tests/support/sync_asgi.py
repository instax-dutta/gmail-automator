"""Synchronous in-process HTTP client for ASGI apps.

`httpx.ASGITransport` is async-only, but the gateway core is deliberately synchronous (master plan
R4), so `OAuthService` and the Gmail transport are exercised through this adapter instead. It binds
no ports, which keeps contract tests fast and lets them assert on the fake app's recorded requests.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx


class SyncASGITransport(httpx.BaseTransport):
    def __init__(self, app: Any) -> None:
        self.app = app

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        body = request.read()
        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": request.method,
            "scheme": request.url.scheme,
            "path": request.url.path,
            "raw_path": request.url.raw_path.split(b"?")[0],
            "query_string": request.url.query,
            "root_path": "",
            "headers": [
                (k.decode("latin-1").lower().encode("latin-1"), v) for k, v in request.headers.raw
            ],
            "client": ("127.0.0.1", 12345),
            "server": (request.url.host, request.url.port),
        }

        state: dict[str, Any] = {"status": 500, "headers": []}
        chunks: list[bytes] = []

        async def receive() -> dict[str, Any]:
            return {"type": "http.request", "body": body, "more_body": False}

        async def send(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                state["status"] = message["status"]
                state["headers"] = message.get("headers", [])
            elif message["type"] == "http.response.body":
                chunks.append(message.get("body", b"") or b"")

        asyncio.run(self.app(scope, receive, send))
        return httpx.Response(
            state["status"],
            headers=state["headers"],
            content=b"".join(chunks),
            request=request,
        )


def sync_asgi_client(app: Any, *, base_url: str = "http://asgi.test") -> httpx.Client:
    return httpx.Client(transport=SyncASGITransport(app), base_url=base_url, timeout=30.0)
