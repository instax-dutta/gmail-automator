"""Synchronous in-process HTTP client for ASGI apps.

`httpx.ASGITransport` is async-only, but the gateway core is deliberately synchronous (master plan
R4), so `OAuthService` and the Gmail transport are exercised through this adapter instead. It binds
no ports, which keeps contract tests fast and lets them assert on the fake app's recorded requests.
"""

from __future__ import annotations

import asyncio
from http.client import responses as http_reasons
from typing import Any
from urllib.parse import unquote, urlparse

import httplib2
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


class SyncASGIHttplib2(httplib2.Http):
    """httplib2 transport that answers requests from an in-process ASGI app.

    `googleapiclient` speaks httplib2, not httpx, so contract tests for the real Gmail transport
    need this adapter. Still no sockets: the whole suite stays hermetic.
    """

    def __init__(self, app: Any) -> None:
        super().__init__()
        self.app = app

    def request(  # type: ignore[override]
        self,
        uri: str,
        method: str = "GET",
        body: str | bytes | None = None,
        headers: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> tuple[httplib2.Response, bytes]:
        parsed = urlparse(uri)
        # ASGI requires `path` to be percent-decoded; googleapiclient encodes path parameters
        # such as userId, so decoding here is what a real server does.
        path = unquote(parsed.path) or "/"
        query = parsed.query.encode()
        raw_headers = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
        raw_headers += [(b"host", (parsed.netloc or "asgi.test").encode())]
        if body is not None and not any(k == b"content-type" for k, _ in raw_headers):
            raw_headers.append((b"content-type", b"application/json"))
        payload = body.encode() if isinstance(body, str) else (body or b"")

        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": method.upper(),
            "scheme": parsed.scheme or "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": query,
            "root_path": "",
            "headers": raw_headers,
            "client": ("127.0.0.1", 12345),
            "server": (parsed.hostname or "asgi.test", parsed.port or 80),
        }

        state: dict[str, Any] = {"status": 500, "headers": []}
        chunks: list[bytes] = []

        async def receive() -> dict[str, Any]:
            return {"type": "http.request", "body": payload, "more_body": False}

        async def send(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                state["status"] = message["status"]
                state["headers"] = list(message.get("headers") or [])
            elif message["type"] == "http.response.body":
                chunks.append(message.get("body", b"") or b"")

        asyncio.run(self.app(scope, receive, send))

        response = httplib2.Response({"status": str(state["status"]), "from-cache": False})
        response.status = state["status"]
        response.reason = http_reasons.get(state["status"], "Unknown")
        response.version = 11
        response.headers = httplib2.Response({k.decode(): v.decode() for k, v in state["headers"]})
        return response, b"".join(chunks)
