"""MCP tools.

Every tool is a thin adapter over the same services the REST API uses: identical validation,
identical quota policy, identical error codes. Agents get one tool surface and one set of rules,
whether they speak MCP or HTTP.

Services are synchronous (master plan R4), so each tool body is dispatched to a worker thread
with `anyio.to_thread.run_sync` and a GatewayError is translated into a `ToolError` so the model
sees an actionable message instead of a stack trace.
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from datetime import datetime
from typing import Any

import anyio
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field, ValidationError

from fmaiily.container import Container, require
from fmaiily.errors import GatewayError
from fmaiily.gmail.mime import OutgoingMessage
from fmaiily.schemas import AccountSummary, to_outgoing_message
from fmaiily.send import SendOutcome, SendService

INSTRUCTIONS = """\
Send email from the operator's own Gmail or Google Workspace accounts.

The gateway enforces Gmail's official rolling 24 hour limits and paces every account, so a send
is refused with `quota_exceeded` rather than risking the account. Always check `get_quota_status`
before a large batch, and prefer `wait=false` for anything longer than a handful of messages: a
blocking call ties up the caller until the worker finishes.
"""

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=True)
MUTATING = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True
)
DESTRUCTIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=True)


# --------------------------------------------------------------------- results


class QuotaToolResult(BaseModel):
    account: str
    messages_sent: int
    recipients_sent: int
    message_soft_limit: int
    recipient_soft_limit: int
    messages_remaining: int
    recipients_remaining: int
    pending_jobs: int
    queue_depth: int
    window_hours: float
    reset_at: datetime | None
    next_send_at: datetime | None


class SendToolResult(BaseModel):
    job_id: int
    status: str
    account: str
    message_id: str | None = None
    recipients: int = 1
    error_code: str | None = None
    error_message: str | None = None
    messages_remaining: int | None = None


class BatchToolResult(BaseModel):
    results: list[SendToolResult]
    sent: int
    queued: int
    failed: int


class AccountToolResult(BaseModel):
    accounts: list[AccountSummary]


class HistoryToolResult(BaseModel):
    items: list[dict[str, Any]]


class AuthUrlToolResult(BaseModel):
    authorization_url: str
    state: str
    expires_at: str


class DisconnectToolResult(BaseModel):
    account: str
    status: str


# ------------------------------------------------------------------- plumbing


def _validated(model: type[BaseModel], payload: dict[str, Any]) -> Any:
    """Validate tool input, turning pydantic's error into an agent-readable ToolError.

    Without this the SDK reports an opaque "Error executing tool <name>" and the model never
    learns which field was wrong.
    """
    try:
        return model.model_validate(payload)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in item['loc'])}: {item['msg']}" for item in exc.errors()
        )
        raise ToolError(f"invalid_request: {problems}") from exc


async def _call[T](fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Run a synchronous service call off the event loop.

    Domain errors become ToolError so a model sees an actionable "code: message" string instead
    of a stack trace.
    """
    try:
        return await anyio.to_thread.run_sync(functools.partial(fn, *args, **kwargs))
    except GatewayError as exc:
        raise ToolError(f"{exc.code}: {exc.message}") from exc


def create_mcp_server(get_container: Callable[[], Container]) -> MCPServer:
    """Build the tool surface. `get_container` is a callable so the tools read the live container
    rather than a snapshot taken before startup finished wiring it."""
    mcp = MCPServer(name="fmaiily", version=_version(), instructions=INSTRUCTIONS)

    def container() -> Container:
        return get_container()

    def sender() -> SendService:
        found: SendService = require(container(), "sender")
        return found

    # ------------------------------------------------------------------ send

    @mcp.tool(
        description=(
            "Send an email from a connected Gmail account. Honours the account's rolling 24 hour "
            "quota and pacing; returns a Gmail message id when it has been delivered, or a job id "
            "when it is still queued."
        ),
        annotations=MUTATING,
    )
    async def send_email(
        to: list[str],
        subject: str,
        body: str,
        account: str | None = None,
        body_html: str | None = None,
        cc: list[str] | None = None,
        bcc: list[str] | None = None,
        reply_to: str | None = None,
        in_reply_to: str | None = None,
        references: str | None = None,
        thread_id: str | None = None,
        idempotency_key: str | None = Field(default=None, max_length=120),
        wait: bool = True,
    ) -> SendToolResult:
        """Queue or send one message.

        `wait=false` returns as soon as the job is queued, which is the right choice for bulk
        work; the job id can be polled with `get_send_status`.
        """
        from fmaiily.schemas import SendEmailRequest

        request = _validated(
            SendEmailRequest,
            {
                "to": to,
                "subject": subject,
                "body": body,
                "body_html": body_html,
                "cc": cc or [],
                "bcc": bcc or [],
                "reply_to": reply_to,
                "in_reply_to": in_reply_to,
                "references": references,
                "thread_id": thread_id,
                "idempotency_key": idempotency_key,
                "wait": wait,
            },
        )
        return await _send(request, account=account, source="mcp")

    async def _send(request: Any, *, account: str | None, source: str) -> SendToolResult:
        resolved = await _call(require(container(), "accounts").resolve, account)
        message: OutgoingMessage = to_outgoing_message(request, resolved.email)
        outcome: SendOutcome = await _call(
            sender().send,
            account_email=resolved.email,
            msg=message,
            source=source,
            idempotency_key=request.idempotency_key,
            wait=request.wait,
            thread_id=request.thread_id,
        )
        return _to_tool_result(
            outcome, recipients=len(message.to) + len(message.cc) + len(message.bcc)
        )

    @mcp.tool(
        description=(
            "Send several emails from one account. The whole batch is validated and quota-checked "
            "before anything is queued, so a refusal leaves the queue untouched. Pacing is applied "
            "per account, so the messages go out `send_interval_seconds` apart."
        ),
        annotations=MUTATING,
    )
    async def send_batch(
        emails: list[dict[str, Any]],
        account: str | None = None,
        wait: bool = False,
    ) -> BatchToolResult:
        """Queue up to 50 messages in one call. Returns one result per input message."""
        from fmaiily.schemas import SendEmailRequest

        requests = [_validated(SendEmailRequest, item) for item in emails]
        resolved = await _call(require(container(), "accounts").resolve, account)
        messages = [to_outgoing_message(item, resolved.email) for item in requests]
        outcome = await _call(
            sender().send_batch,
            account_email=resolved.email,
            messages=messages,
            source="mcp",
            wait=wait,
        )
        results = [
            _to_tool_result(item, recipients=_recipient_count(msg))
            for item, msg in zip(outcome.outcomes, messages, strict=True)
        ]
        return BatchToolResult(
            results=results,
            sent=sum(1 for r in results if r.status == "sent"),
            queued=sum(1 for r in results if r.status == "queued"),
            failed=sum(1 for r in results if r.status == "failed"),
        )

    # ------------------------------------------------------------------ quota

    @mcp.tool(
        description=(
            "Remaining capacity for the rolling 24 hour window: messages and recipients still "
            "available, jobs already reserved, queue depth, and when capacity frees up. Check this "
            "before a large batch."
        ),
        annotations=READ_ONLY,
    )
    async def get_quota_status(account: str | None = None) -> QuotaToolResult:
        resolved = await _call(require(container(), "accounts").resolve, account)
        snapshot = await _call(
            require(container(), "quota").snapshot, resolved, now=container().clock.now()
        )
        return QuotaToolResult(
            account=snapshot.account_email,
            messages_sent=snapshot.messages_sent,
            recipients_sent=snapshot.recipients_sent,
            message_soft_limit=snapshot.message_soft_limit,
            recipient_soft_limit=snapshot.recipient_soft_limit,
            messages_remaining=snapshot.messages_remaining,
            recipients_remaining=snapshot.recipients_remaining,
            pending_jobs=snapshot.pending_jobs,
            queue_depth=snapshot.queue_depth,
            window_hours=snapshot.window_hours,
            reset_at=snapshot.reset_at,
            next_send_at=snapshot.next_send_at,
        )

    # --------------------------------------------------------------- accounts

    @mcp.tool(
        description="List connected Gmail accounts with their status and granted scopes.",
        annotations=READ_ONLY,
    )
    async def list_accounts() -> AccountToolResult:
        return AccountToolResult(accounts=await _call(require(container(), "accounts").summaries))

    @mcp.tool(
        description=(
            "Return the Google consent URL for connecting a new Gmail account. A human must "
            "open it in a browser; the gateway never opens one itself."
        ),
        annotations=MUTATING,
    )
    async def start_account_connect(login_hint: str | None = None) -> AuthUrlToolResult:
        request = await _call(require(container(), "oauth").start, account_hint=login_hint)
        return AuthUrlToolResult(
            authorization_url=request.authorization_url,
            state=request.state,
            expires_at=request.expires_at.isoformat(),
        )

    @mcp.tool(
        description=(
            "Disconnect an account and delete its stored tokens. Any send already queued for it "
            "will fail."
        ),
        annotations=DESTRUCTIVE,
    )
    async def disconnect_account(account: str) -> DisconnectToolResult:
        await _call(require(container(), "oauth").revoke, account)
        return DisconnectToolResult(account=account, status="revoked")

    # ---------------------------------------------------------------- history

    @mcp.tool(
        description="Recent send jobs, newest first, with status, recipient counts, and errors.",
        annotations=READ_ONLY,
    )
    async def get_send_history(
        account: str | None = None, limit: int = 20, status: str | None = None
    ) -> HistoryToolResult:
        items = await _call(
            require(container(), "history").list_recent,
            account_email=account,
            status=status,
            limit=max(1, min(limit, 200)),
        )
        return HistoryToolResult(items=[item.model_dump(mode="json") for item in items])

    @mcp.tool(
        description=(
            "Full status of one send job, including the Gmail message id or the error code."
        ),
        annotations=READ_ONLY,
    )
    async def get_send_status(job_id: int) -> dict[str, Any]:
        from fmaiily.schemas import JobStatusResponse

        status: JobStatusResponse | None = await _call(
            require(container(), "history").job_status, job_id
        )
        if status is None:
            raise ToolError(f"job_not_found: no send job with id {job_id}")
        return status.model_dump(mode="json")

    return mcp


def _recipient_count(message: OutgoingMessage) -> int:
    from fmaiily.gmail.mime import count_recipients

    return count_recipients(message)


def _to_tool_result(outcome: SendOutcome, *, recipients: int) -> SendToolResult:
    return SendToolResult(
        job_id=outcome.job_id,
        status=outcome.status,
        account=outcome.account_email,
        message_id=outcome.message_id,
        recipients=recipients,
        error_code=outcome.error_code,
        error_message=outcome.error_message,
        messages_remaining=outcome.quota.messages_remaining if outcome.quota else None,
    )


def _version() -> str:
    from fmaiily import __version__

    return __version__


def serve_stdio(get_container: Callable[[], Container]) -> None:
    """Run the MCP server over stdio, the transport local agent clients spawn as a subprocess."""
    configure_logging_for_stdio()
    create_mcp_server(get_container).run(transport="stdio")


def configure_logging_for_stdio() -> None:
    """stdio is a protocol channel: human-readable logs on stdout would corrupt the stream."""
    from fmaiily.logging_setup import configure_logging

    configure_logging(level="WARNING", json_output=False)


__all__ = [
    "AccountToolResult",
    "AuthUrlToolResult",
    "BatchToolResult",
    "DisconnectToolResult",
    "HistoryToolResult",
    "QuotaToolResult",
    "SendToolResult",
    "create_mcp_server",
    "serve_stdio",
]
