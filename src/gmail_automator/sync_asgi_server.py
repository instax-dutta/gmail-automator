"""Run an ASGI app on a real port in a background thread.

Contract tests drive services in-process through `tests/support/sync_asgi.py`; the end-to-end
tests need a real socket so uvicorn, the ASGI middleware, and the MCP mount are all exercised.
"""

from __future__ import annotations

import threading
import time
from typing import Any

import uvicorn


class BackgroundServer:
    def __init__(self, server: uvicorn.Server, thread: threading.Thread) -> None:
        self._server = server
        self._thread = thread

    @property
    def port(self) -> int:
        return int(self._server.servers[0].sockets[0].getsockname()[1])

    def stop(self, timeout: float = 10.0) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=timeout)


def serve_asgi_in_thread(
    app: Any,
    *,
    host: str = "127.0.0.1",
    port: int = 0,
    log_level: str = "warning",
    wait_timeout: float = 10.0,
) -> BackgroundServer:
    """Serve `app` on a real port, returning once it is accepting connections."""
    config = uvicorn.Config(app, host=host, port=port, log_level=log_level, lifespan="on")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.monotonic() + wait_timeout
    while time.monotonic() < deadline:
        if getattr(server, "started", False):
            return BackgroundServer(server, thread)
        time.sleep(0.02)
    server.should_exit = True
    thread.join(timeout=wait_timeout)
    raise TimeoutError(f"server on {host}:{port} did not start within {wait_timeout}s")


__all__ = ["BackgroundServer", "serve_asgi_in_thread"]
