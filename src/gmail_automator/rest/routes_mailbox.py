"""Mailbox read/organise and reply over REST, mirroring the MCP tools exactly.

Both surfaces are thin adapters over the same services, so a quota, scope, or threading rule cannot
differ depending on how an agent reached the gateway. That is the whole point of having two
front doors onto one policy.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from gmail_automator.container import require
from gmail_automator.rest.deps import CallerDep, ContainerDep, authenticate
from gmail_automator.schemas import ErrorBody

#: Every /v1 router requires a resolved caller.
AUTH = [Depends(authenticate)]

router = APIRouter(prefix="/v1", dependencies=AUTH, tags=["mailbox"])

#: The error codes these routes can return, so the OpenAPI page and an agent-generated client
#: both see the envelope rather than a bare 4xx.
ErrorResponses: dict[int | str, dict[str, Any]] = {
    400: {"model": ErrorBody},
    403: {"model": ErrorBody},
    404: {"model": ErrorBody},
}


# ------------------------------------------------------------------ responses


class MessageSummaryModel(BaseModel):
    id: str
    thread_id: str | None = None
    subject: str = ""
    sender: str = ""
    recipients: str = ""
    date: str = ""
    snippet: str = ""
    label_ids: list[str] = Field(default_factory=list)


class MessagePageResponse(BaseModel):
    messages: list[MessageSummaryModel]
    next_page_token: str | None = None
    result_size_estimate: int = 0


class MessageDetailResponse(MessageSummaryModel):
    message_id_header: str | None = None
    in_reply_to: str | None = None
    references: str | None = None
    body_text: str | None = None
    body_html: str | None = None


class LabelModel(BaseModel):
    id: str
    name: str
    type: str
    messages_total: int
    messages_unread: int


class LabelListResponse(BaseModel):
    labels: list[LabelModel]


class LabelResultResponse(BaseModel):
    message_id: str
    label_ids: list[str]


class ReplyRequest(BaseModel):
    """Reply to a message. Exactly one of `job_id` or `message_id` identifies the target.

    `job_id` addresses a message this gateway sent and needs no mailbox read scope; `message_id`
    addresses any message the account can read.
    """

    body: str = Field(min_length=1, max_length=1048576)
    job_id: int | None = Field(default=None, ge=1)
    message_id: str | None = None
    to: list[str] | None = None
    cc: list[str] | None = None
    bcc: list[str] | None = None
    body_html: str | None = None
    subject: str | None = None
    draft: bool = False
    wait: bool = True
    account: str | None = None


class ReplyResponse(BaseModel):
    kind: str
    account: str
    replied_to: str
    thread_id: str | None = None
    job_id: int | None = None
    status: str | None = None
    message_id: str | None = None
    draft_id: str | None = None


# --------------------------------------------------------------------- routes


@router.get(
    "/mailbox/messages",
    response_model=MessagePageResponse,
    summary="Search the mailbox (Gmail query syntax)",
    responses=ErrorResponses,
)
def list_messages(
    container: ContainerDep,
    caller: CallerDep,
    query: Annotated[str | None, Query(description="Gmail search syntax, passed through")] = None,
    max_results: Annotated[int, Query(ge=1, le=100)] = 10,
    page_token: str | None = None,
    account: str | None = None,
) -> MessagePageResponse:
    page = require(container, "mailbox").list_messages(
        account_email=account,
        query=query,
        max_results=max_results,
        page_token=page_token,
    )
    return MessagePageResponse(
        messages=[MessageSummaryModel(**vars(m)) for m in page.messages],
        next_page_token=page.next_page_token,
        result_size_estimate=page.result_size_estimate,
    )


@router.get(
    "/mailbox/messages/{message_id}",
    response_model=MessageDetailResponse,
    summary="Read one message with its body and threading headers",
    responses=ErrorResponses,
)
def read_message(
    container: ContainerDep,
    caller: CallerDep,
    message_id: str,
    account: str | None = None,
) -> MessageDetailResponse:
    detail = require(container, "mailbox").get_message(message_id=message_id, account_email=account)
    return MessageDetailResponse(
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


@router.get(
    "/mailbox/labels",
    response_model=LabelListResponse,
    summary="List every label, system and custom",
    responses=ErrorResponses,
)
def list_labels(
    container: ContainerDep,
    caller: CallerDep,
    account: str | None = None,
) -> LabelListResponse:
    labels = require(container, "mailbox").list_labels(account_email=account)
    return LabelListResponse(labels=[LabelModel(**vars(label)) for label in labels])


@router.post(
    "/mailbox/messages/{message_id}/labels",
    response_model=LabelResultResponse,
    summary="Change a message's labels",
    responses=ErrorResponses,
)
def modify_message(
    container: ContainerDep,
    caller: CallerDep,
    message_id: str,
    states: Annotated[list[str] | None, Query()] = None,
    add_labels: Annotated[list[str] | None, Query()] = None,
    remove_labels: Annotated[list[str] | None, Query()] = None,
    account: str | None = None,
) -> LabelResultResponse:
    result = require(container, "mailbox").modify_message(
        message_id=message_id,
        states=tuple(states or ()),
        add_labels=tuple(add_labels or ()),
        remove_labels=tuple(remove_labels or ()),
        account_email=account,
    )
    return LabelResultResponse(message_id=result.id, label_ids=list(result.label_ids))


@router.post(
    "/reply",
    response_model=ReplyResponse,
    summary="Reply to a message so it threads correctly, or draft the reply",
    responses=ErrorResponses,
)
def reply(
    payload: ReplyRequest,
    container: ContainerDep,
    caller: CallerDep,
) -> ReplyResponse:
    outcome = require(container, "replies").reply(
        account_email=payload.account,
        job_id=payload.job_id,
        message_id=payload.message_id,
        to=tuple(payload.to) if payload.to else None,
        cc=tuple(payload.cc) if payload.cc else None,
        bcc=tuple(payload.bcc) if payload.bcc else None,
        body=payload.body,
        body_html=payload.body_html,
        subject=payload.subject,
        draft=payload.draft,
        wait=payload.wait,
    )
    return ReplyResponse(
        kind=outcome.kind,
        account=outcome.account,
        replied_to=outcome.replied_to,
        thread_id=outcome.thread_id,
        job_id=outcome.job_id,
        status=outcome.status,
        message_id=outcome.message_id,
        draft_id=outcome.draft_id,
    )


__all__ = [
    "LabelListResponse",
    "LabelModel",
    "LabelResultResponse",
    "MessageDetailResponse",
    "MessagePageResponse",
    "MessageSummaryModel",
    "ReplyRequest",
    "ReplyResponse",
    "router",
]
