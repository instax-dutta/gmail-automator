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

from gmail_automator.container import Container, require
from gmail_automator.drafts import COMPOSE_SCOPES, SEND_SCOPE
from gmail_automator.errors import GatewayError
from gmail_automator.gmail.mime import OutgoingMessage
from gmail_automator.schemas import AccountSummary, to_outgoing_message
from gmail_automator.send import SendOutcome, SendService, request_fingerprint

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


class MessageSummaryModel(BaseModel):
    id: str
    thread_id: str | None = None
    subject: str = ""
    sender: str = ""
    recipients: str = ""
    date: str = ""
    snippet: str = ""
    label_ids: list[str] = Field(default_factory=list)


class MessagePageModel(BaseModel):
    messages: list[MessageSummaryModel]
    next_page_token: str | None = None
    result_size_estimate: int = 0


class MessageDetailModel(BaseModel):
    id: str
    thread_id: str | None = None
    subject: str = ""
    sender: str = ""
    recipients: str = ""
    date: str = ""
    snippet: str = ""
    label_ids: list[str] = Field(default_factory=list)
    message_id_header: str | None = None
    in_reply_to: str | None = None
    references: str | None = None
    body_text: str | None = None
    body_html: str | None = None


class LabelResultModel(BaseModel):
    message_id: str
    label_ids: list[str]


class ReplyToolResult(BaseModel):
    kind: str
    account: str
    replied_to: str
    thread_id: str | None = None
    job_id: int | None = None
    status: str | None = None
    message_id: str | None = None
    draft_id: str | None = None
    error_code: str | None = None
    error_message: str | None = None


class DraftToolResult(BaseModel):
    draft_id: str
    message_id: str | None
    thread_id: str | None
    account: str


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
    mcp = MCPServer(name="gmail_automator", version=_version(), instructions=INSTRUCTIONS)

    def container() -> Container:
        return get_container()

    def sender() -> SendService:
        found: SendService = require(container(), "sender")
        return found

    # ------------------------------------------------------------------ send

    @mcp.tool(
        description=(
            "Send an email from a connected Gmail account. PRECONDITION: an account must already "
            f"be connected with {SEND_SCOPE}. Honours the account's rolling 24 hour quota and "
            "pacing, and a send that would exceed them is refused before Google is contacted. "
            "wait=true returns a Gmail message id once delivered; wait=false returns a job id "
            "immediately. The result always carries messages_remaining, so there is no need to "
            "ask for the budget separately."
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
        from gmail_automator.schemas import SendEmailRequest

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
        message: OutgoingMessage = to_outgoing_message(
            request, resolved.email, settings=container().settings
        )
        outcome: SendOutcome = await _call(
            sender().send,
            account_email=resolved.email,
            msg=message,
            source=source,
            idempotency_key=request.idempotency_key,
            request_hash=(request_fingerprint(request) if request.idempotency_key else None),
            wait=request.wait,
            thread_id=request.thread_id,
        )
        return _to_tool_result(
            outcome, recipients=len(message.to) + len(message.cc) + len(message.bcc)
        )

    @mcp.tool(
        description=(
            "Send several emails from one account. The whole batch is validated and quota-checked "
            "before anything is queued, so a refusal leaves the queue untouched and no partial "
            "send happens. Pacing is applied per account, so the messages go out "
            "`send_interval_seconds` apart rather than all at once. PRECONDITION: an account "
            f"connected with {SEND_SCOPE}. Call get_quota_status first for a large batch; a batch "
            "larger than the remaining budget is refused whole."
        ),
        annotations=MUTATING,
    )
    async def send_batch(
        emails: list[dict[str, Any]],
        account: str | None = None,
        wait: bool = False,
    ) -> BatchToolResult:
        """Queue up to 50 messages in one call. Returns one result per input message."""
        from gmail_automator.schemas import SendEmailRequest

        requests = [_validated(SendEmailRequest, item) for item in emails]
        resolved = await _call(require(container(), "accounts").resolve, account)
        messages = [
            to_outgoing_message(item, resolved.email, settings=container().settings)
            for item in requests
        ]
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
        description=(
            "List connected Gmail accounts with their status, account type, and granted OAuth "
            "scopes. This is how to check a precondition before another tool: a send needs "
            f"{SEND_SCOPE}, and a draft needs {' or '.join(COMPOSE_SCOPES)}."
        ),
        annotations=READ_ONLY,
    )
    async def list_accounts() -> AccountToolResult:
        return AccountToolResult(accounts=await _call(require(container(), "accounts").summaries))

    @mcp.tool(
        description=(
            "Return the Google consent URL for connecting a new Gmail account. A human must open "
            "it in a browser and approve the consent screen; the gateway never opens one itself, "
            "and the URL expires, so a fresh call is needed per attempt. PRECONDITION: the gateway "
            "must have an OAuth client configured, otherwise this returns a not-configured error."
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
            "Disconnect an account and permanently delete its stored tokens; they cannot be "
            "recovered and the account must be reconnected by a human. Any send already queued "
            "for it will fail."
        ),
        annotations=DESTRUCTIVE,
    )
    async def disconnect_account(account: str) -> DisconnectToolResult:
        await _call(require(container(), "oauth").revoke, account)
        return DisconnectToolResult(account=account, status="revoked")

    @mcp.tool(
        description=(
            f"Create a draft instead of sending, so a human can review it in Gmail. Consumes no "
            f"quota and creates no job. PRECONDITION: the account must hold "
            f"{' or '.join(COMPOSE_SCOPES)}; an account connected with "
            f"{SEND_SCOPE} alone is refused with scope_missing. Call list_accounts first to see "
            f"which scopes an account actually holds."
        ),
        annotations=MUTATING,
    )
    async def create_draft(
        to: list[str],
        subject: str,
        body: str,
        account: str | None = None,
        body_html: str | None = None,
        cc: list[str] | None = None,
        thread_id: str | None = None,
    ) -> DraftToolResult:
        """Leave the message in the mailbox rather than sending it."""
        from gmail_automator.drafts import DraftService
        from gmail_automator.schemas import SendEmailRequest

        request = _validated(
            SendEmailRequest,
            {
                "to": to,
                "subject": subject,
                "body": body,
                "body_html": body_html,
                "cc": cc or [],
                "thread_id": thread_id,
                "wait": False,
            },
        )
        drafts: DraftService = require(container(), "drafts")
        resolved = await _call(require(container(), "accounts").resolve, account)
        message = to_outgoing_message(request, resolved.email, settings=container().settings)
        result = await _call(
            drafts.create_draft,
            account_email=resolved.email,
            msg=message,
            thread_id=request.thread_id,
        )
        return DraftToolResult(
            draft_id=result.draft_id,
            message_id=result.message_id,
            thread_id=result.thread_id,
            account=resolved.email,
        )

    # ---------------------------------------------------------------- mailbox

    @mcp.tool(
        description=(
            "Search the mailbox. `query` uses Gmail's own search syntax and is passed through "
            "unchanged, so `from:`, `subject:`, `newer_than:7d`, `has:attachment`, `is:unread`, "
            "and AND/OR work as they do in the Gmail search box. Results carry a snippet, not the "
            "body: use read_message for that. Returns next_page_token when more exist, so pass it "
            "back to continue. PRECONDITION: the account needs a read scope "
            "(gmail.modify or gmail.readonly); a send-only account gets scope_missing."
        ),
        annotations=READ_ONLY,
    )
    async def list_messages(
        query: str | None = None,
        max_results: int = 10,
        page_token: str | None = None,
        account: str | None = None,
    ) -> MessagePageModel:
        page = await _call(
            require(container(), "mailbox").list_messages,
            account_email=account,
            query=query,
            max_results=max(1, min(max_results, 100)),
            page_token=page_token,
        )
        return MessagePageModel(
            messages=[MessageSummaryModel(**vars(m)) for m in page.messages],
            next_page_token=page.next_page_token,
            result_size_estimate=page.result_size_estimate,
        )

    @mcp.tool(
        description=(
            "Read one message in full: decoded headers, body_text (preferred), body_html, and the "
            "threading headers. Message ids come from list_messages or get_send_status. Bodies are "
            "base64url in Gmail's API and decoded here, and RFC 2047 headers are decoded, so the "
            "result is readable rather than raw. PRECONDITION: needs a read scope."
        ),
        annotations=READ_ONLY,
    )
    async def read_message(
        message_id: str,
        account: str | None = None,
    ) -> MessageDetailModel:
        detail = await _call(
            require(container(), "mailbox").get_message,
            message_id=message_id,
            account_email=account,
        )
        return MessageDetailModel(
            id=detail.id,
            thread_id=detail.thread_id,
            subject=detail.subject,
            sender=detail.sender,
            recipients=detail.recipients,
            date=detail.date,
            snippet=detail.snippet,
            label_ids=list(detail.label_ids),
            message_id_header=detail.message_id_header,
            in_reply_to=detail.in_reply_to,
            references=detail.references,
            body_text=detail.body_text,
            body_html=detail.body_html,
        )

    @mcp.tool(
        description=(
            "List every label on the account, system and custom, with total and unread counts. "
            "Use this before modify_message so a message is filed under a label the person "
            "actually created, rather than one invented on the spot. PRECONDITION: needs "
            "gmail.modify."
        ),
        annotations=READ_ONLY,
    )
    async def list_labels(account: str | None = None) -> dict[str, Any]:
        labels = await _call(require(container(), "mailbox").list_labels, account_email=account)
        return {
            "labels": [
                {
                    "id": label.id,
                    "name": label.name,
                    "type": label.type,
                    "messages_total": label.messages_total,
                    "messages_unread": label.messages_unread,
                }
                for label in labels
            ]
        }

    @mcp.tool(
        description=(
            "Organise a message by changing its labels. `states` takes friendly names rather than "
            "Gmail label ids: read, unread, starred, unstarred, archived, in_inbox, trashed, "
            "not_trashed, important, not_important, not_spam. `add_labels`/`remove_labels` set "
            "custom labels by name. Archiving is removing INBOX; trashing is reversible, and there "
            "is deliberately no permanent delete. PRECONDITION: needs gmail.modify."
        ),
        annotations=MUTATING,
    )
    async def modify_message(
        message_id: str,
        states: list[str] | None = None,
        add_labels: list[str] | None = None,
        remove_labels: list[str] | None = None,
        account: str | None = None,
    ) -> LabelResultModel:
        result = await _call(
            require(container(), "mailbox").modify_message,
            message_id=message_id,
            states=tuple(states or ()),
            add_labels=tuple(add_labels or ()),
            remove_labels=tuple(remove_labels or ()),
            account_email=account,
        )
        return LabelResultModel(message_id=result.id, label_ids=list(result.label_ids))

    @mcp.tool(
        description=(
            "Reply to a message so the reply threads correctly in Gmail. Identify the target with "
            "exactly one of: `job_id` for a message this gateway sent (from get_send_status or "
            "get_send_history), or `message_id` for any message the account can read (from "
            "list_messages). The recipient, subject, In-Reply-To, References, and Gmail thread id "
            "are all derived from the original, so the caller supplies only the body; `to` "
            "overrides the recipient. Set `draft=true` to leave the reply in Gmail for a human to "
            "review instead of sending it. PRECONDITION: reading the original needs a read scope; "
            "drafting also needs a compose scope."
        ),
        annotations=MUTATING,
    )
    async def reply(
        body: str,
        job_id: int | None = None,
        message_id: str | None = None,
        to: list[str] | None = None,
        cc: list[str] | None = None,
        bcc: list[str] | None = None,
        body_html: str | None = None,
        subject: str | None = None,
        draft: bool = False,
        wait: bool = True,
        account: str | None = None,
    ) -> ReplyToolResult:
        outcome = await _call(
            require(container(), "replies").reply,
            account_email=account,
            job_id=job_id,
            message_id=message_id,
            to=tuple(to) if to else None,
            cc=tuple(cc) if cc else None,
            bcc=tuple(bcc) if bcc else None,
            body=body,
            body_html=body_html,
            subject=subject,
            draft=draft,
            wait=wait,
        )
        return ReplyToolResult(
            kind=outcome.kind,
            account=outcome.account,
            replied_to=outcome.replied_to,
            thread_id=outcome.thread_id,
            job_id=outcome.job_id,
            status=outcome.status,
            message_id=outcome.message_id,
            draft_id=outcome.draft_id,
            error_code=outcome.error_code,
            error_message=outcome.error_message,
        )

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
            "Full status of one send job, including the Gmail message id or the typed error code. "
            "Use the job_id returned by send_email or send_batch; there is no way to look a job up "
            "by recipient or subject."
        ),
        annotations=READ_ONLY,
    )
    async def get_send_status(job_id: int) -> dict[str, Any]:
        from gmail_automator.schemas import JobStatusResponse

        status: JobStatusResponse | None = await _call(
            require(container(), "history").job_status, job_id
        )
        if status is None:
            raise ToolError(f"job_not_found: no send job with id {job_id}")
        return status.model_dump(mode="json")

    return mcp


def _recipient_count(message: OutgoingMessage) -> int:
    from gmail_automator.gmail.mime import count_recipients

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
    from gmail_automator import __version__

    return __version__


def serve_stdio(get_container: Callable[[], Container]) -> None:
    """Run the MCP server over stdio, the transport local agent clients spawn as a subprocess."""
    configure_logging_for_stdio()
    create_mcp_server(get_container).run(transport="stdio")


def configure_logging_for_stdio() -> None:
    """stdio is a protocol channel: human-readable logs on stdout would corrupt the stream."""
    from gmail_automator.logging_setup import configure_logging

    configure_logging(level="WARNING", json_output=False)


__all__ = [
    "AccountToolResult",
    "AuthUrlToolResult",
    "BatchToolResult",
    "DisconnectToolResult",
    "DraftToolResult",
    "HistoryToolResult",
    "QuotaToolResult",
    "SendToolResult",
    "create_mcp_server",
    "serve_stdio",
]
