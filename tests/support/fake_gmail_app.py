from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from tests.support.gmail_payload import gmail_payload

DEFAULT_ACCOUNT = "sender@example.com"


def fake_gmail_app() -> FastAPI:
    """In-process fake: OAuth token exchange/refresh, OIDC userinfo, Gmail send/drafts, and the
    mailbox read/organise surface (messages.list, messages.get, messages.modify, labels.list).

    The mailbox endpoints answer from `app.state.mailbox`, so a test can seed a message with a
    real MIME body and threading headers and assert that a reply picks them up.
    """
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
        rotated = app.state.behavior.get("rotate_refresh_token")
        if rotated:
            payload["refresh_token"] = rotated
        elif not app.state.behavior.get("omit_refresh_token"):
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

    # ------------------------------------------------------------------ mailbox

    def _mailbox() -> dict[str, Any]:
        return dict(app.state.mailbox)

    @app.get("/gmail/v1/users/{user_id}/messages")
    async def list_messages(user_id: str, request: Request) -> JSONResponse:
        query = request.query_params.get("q")
        max_results = int(request.query_params.get("maxResults", 10))
        page_token = request.query_params.get("pageToken")
        app.state.requests.append(
            {
                "path": "messages.list",
                "user_id": user_id,
                "q": query,
                "max_results": max_results,
                "page_token": page_token,
            }
        )
        store = _mailbox()
        messages = list(store.get("messages", []))
        if query:
            messages = [m for m in messages if _matches(m, query)]
        start = 0
        if page_token:
            try:
                start = int(page_token)
            except ValueError:
                start = 0
        window = messages[start : start + max_results]
        next_start = start + max_results
        return JSONResponse(
            {
                # A bare id and a thread id, which is all this endpoint populates. Returning a
                # snippet or label ids here would let a wrong read of them pass unnoticed.
                "messages": [{"id": m["id"], "threadId": m.get("threadId")} for m in window],
                "nextPageToken": str(next_start) if next_start < len(messages) else None,
                "resultSizeEstimate": len(messages),
            }
        )

    @app.get("/gmail/v1/users/{user_id}/messages/{message_id}")
    async def get_message(user_id: str, message_id: str) -> JSONResponse:
        app.state.requests.append(
            {"path": "messages.get", "user_id": user_id, "message_id": message_id}
        )
        store = _mailbox()
        for message in store.get("messages", []):
            if message["id"] == message_id:
                # Assembled as a real MIME tree rather than one flat blob: Gmail leaves the root
                # body of a multipart message unset, and a double that always returns a flat blob
                # cannot exercise the multipart path the parser actually has to handle.
                return JSONResponse(
                    gmail_payload(
                        message.get("raw", b""),
                        message_id=message["id"],
                        thread_id=message.get("threadId"),
                        label_ids=tuple(message.get("labelIds", [])),
                        snippet=message.get("snippet", ""),
                        headers=message.get("headers"),
                    )
                )
        return JSONResponse(
            {"error": {"code": 404, "message": "Not Found", "errors": [{"reason": "notFound"}]}},
            status_code=404,
        )

    @app.post("/gmail/v1/users/{user_id}/messages/{message_id}/modify")
    async def modify_message(user_id: str, message_id: str, request: Request) -> JSONResponse:
        body = await request.json()
        app.state.requests.append(
            {
                "path": "messages.modify",
                "user_id": user_id,
                "message_id": message_id,
                "body": body,
            }
        )
        store = _mailbox()
        for message in store.get("messages", []):
            if message["id"] == message_id:
                labels = set(message.get("labelIds", []))
                labels.update(body.get("addLabelIds") or [])
                labels.difference_update(body.get("removeLabelIds") or [])
                message["labelIds"] = sorted(labels)
                return JSONResponse({"id": message_id, "labelIds": sorted(labels)})
        return JSONResponse(
            {"error": {"code": 404, "message": "Not Found", "errors": [{"reason": "notFound"}]}},
            status_code=404,
        )

    @app.get("/gmail/v1/users/{user_id}/labels")
    async def list_labels(user_id: str) -> JSONResponse:
        app.state.requests.append({"path": "labels.list", "user_id": user_id})
        return JSONResponse({"labels": list(_mailbox().get("labels", []))})

    app.state.mailbox = {}
    return app


def _matches(message: dict[str, Any], query: str) -> bool:
    """Support the subset of Gmail search syntax the tests use.

    A full parser is not the point of a fake; supporting `from:`, `subject:`, `is:`, and a bare
    term is enough to prove the query reaches Gmail rather than being mangled in transit.
    """
    haystack = " ".join(
        [
            str(message.get("headers", {}).get("From", "")),
            str(message.get("headers", {}).get("Subject", "")),
            str(message.get("snippet", "")),
            " ".join(message.get("labelIds", [])),
        ]
    ).lower()
    for token in query.split():
        field, _, term = token.partition(":")
        if term and field.lower() in ("from", "subject", "is", "has", "to"):
            if field.lower() == "is":
                if term.lower() not in {label.lower() for label in message.get("labelIds", [])}:
                    return False
            elif term.lower() not in haystack:
                return False
        elif term.lower() not in haystack:
            return False
    return True
