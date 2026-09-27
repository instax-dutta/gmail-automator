from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from gmail_automator.container import require
from gmail_automator.drafts import DraftService
from gmail_automator.rest.deps import CallerDep, ContainerDep, authenticate
from gmail_automator.schemas import SendEmailRequest, to_outgoing_message

router = APIRouter(prefix="/v1/drafts", tags=["drafts"], dependencies=[Depends(authenticate)])


class DraftRequest(BaseModel):
    account: str | None = None
    to: list[str]
    subject: str
    body: str
    body_html: str | None = None
    cc: list[str] = []
    bcc: list[str] = []
    reply_to: str | None = None
    in_reply_to: str | None = None
    references: str | None = None
    thread_id: str | None = None


class DraftResponse(BaseModel):
    draft_id: str
    message_id: str | None
    thread_id: str | None
    account: str


@router.post("", response_model=DraftResponse, summary="Create a draft instead of sending")
def create_draft(
    payload: DraftRequest, container: ContainerDep, caller: CallerDep
) -> DraftResponse:
    """Leave the message in the mailbox for a human to review.

    Requires the account to hold a compose scope; an account connected with only `gmail.send` gets
    `scope_missing` naming the scope to add. A draft consumes no quota and creates no job, because
    nothing is sent.
    """
    drafts: DraftService = require(container, "drafts")
    resolved = require(container, "accounts").resolve(payload.account)
    caller.require_account(resolved.email)
    caller.require_scope("send")
    # A draft carries no send-only fields, so it is expressed with the shared request model.
    request = SendEmailRequest.model_validate(
        {
            "to": payload.to,
            "subject": payload.subject,
            "body": payload.body,
            "body_html": payload.body_html,
            "cc": payload.cc,
            "bcc": payload.bcc,
            "reply_to": payload.reply_to,
            "in_reply_to": payload.in_reply_to,
            "references": payload.references,
            "thread_id": payload.thread_id,
            "wait": False,
        }
    )
    message = to_outgoing_message(request, resolved.email, settings=container.settings)
    result = drafts.create_draft(
        account_email=resolved.email, msg=message, thread_id=payload.thread_id
    )
    return DraftResponse(
        draft_id=result.draft_id,
        message_id=result.message_id,
        thread_id=result.thread_id,
        account=resolved.email,
    )


__all__ = ["DraftRequest", "DraftResponse", "router"]
