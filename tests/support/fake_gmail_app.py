from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

DEFAULT_ACCOUNT = "sender@example.com"


def fake_gmail_app() -> FastAPI:
    """In-process fake for: OAuth token exchange/refresh, OIDC userinfo, Gmail send/drafts."""
    app = FastAPI()
    app.state.requests = []
    app.state.behavior = {}

    @app.post("/token")
    async def token(request: Request) -> JSONResponse:
        form = await request.form()
        app.state.requests.append({"path": "/token", "form": dict(form)})
        behavior: dict[str, Any] = dict(app.state.behavior.get("token", {}))
        if behavior.get("status", 200) != 200:
            return JSONResponse(
                behavior.get(
                    "error_body", {"error": "invalid_grant", "error_description": "bad code"}
                ),
                status_code=behavior["status"],
            )
        payload: dict[str, Any] = {
            "access_token": "fake-access-token",
            "expires_in": 3600,
            "token_type": "Bearer",
            "scope": app.state.behavior.get("token_scopes")
            or " ".join(str(form.get("scope", "")).split()),
        }
        if not app.state.behavior.get("omit_refresh_token"):
            payload["refresh_token"] = form.get("refresh_token") or "fake-refresh-token"
        return JSONResponse(payload)

    @app.get("/v1/userinfo")
    async def userinfo(request: Request) -> JSONResponse:
        app.state.requests.append(
            {"path": "/v1/userinfo", "auth": request.headers.get("authorization")}
        )
        if not str(request.headers.get("authorization", "")).startswith("Bearer "):
            return JSONResponse({"error": "invalid_token"}, status_code=401)
        return JSONResponse(
            {
                "email": app.state.behavior.get("userinfo_email", DEFAULT_ACCOUNT),
                "email_verified": app.state.behavior.get("userinfo_email_verified", True),
                "sub": "123",
            }
        )

    @app.post("/gmail/v1/users/{user_id}/messages/send")
    async def send(user_id: str, request: Request) -> JSONResponse:
        body = await request.json()
        app.state.requests.append(
            {
                "path": "send",
                "user_id": user_id,
                "body": body,
                "auth": request.headers.get("authorization"),
            }
        )
        behavior: dict[str, Any] = dict(app.state.behavior.get("send", {}))
        if behavior:
            if behavior.get("times", 1) <= 0:
                behavior = {}
            else:
                behavior["times"] -= 1
                app.state.behavior["send"] = behavior
        status = behavior.get("status", 200)
        if status != 200:
            return JSONResponse(
                behavior.get(
                    "error_body",
                    {
                        "error": {
                            "code": 429,
                            "message": "Rate Limit Exceeded",
                            "errors": [
                                {"reason": "rateLimitExceeded", "message": "Rate Limit Exceeded"}
                            ],
                        }
                    },
                ),
                status_code=status,
                headers=behavior.get("headers", {}),
            )
        return JSONResponse(
            {
                "id": behavior.get("id", "fake-msg-1"),
                "threadId": body.get("threadId", "fake-thread-1"),
                "labelIds": ["SENT"],
            }
        )

    @app.post("/gmail/v1/users/{user_id}/drafts")
    async def draft(user_id: str, request: Request) -> JSONResponse:
        body = await request.json()
        app.state.requests.append({"path": "draft", "user_id": user_id, "body": body})
        return JSONResponse({"id": "draft-1", "message": {"id": "draft-msg-1"}})

    return app
