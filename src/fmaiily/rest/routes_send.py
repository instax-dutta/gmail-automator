from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from fmaiily.container import require
from fmaiily.rest.deps import CallerDep, ContainerDep, authenticate
from fmaiily.schemas import (
    BatchSendRequest,
    BatchSendResponse,
    SendEmailRequest,
    SendEmailResponse,
    to_outgoing_message,
)
from fmaiily.send import BatchOutcome, SendOutcome, request_fingerprint

#: Every /v1 router requires a resolved caller. Routes that need the identity itself
#: declare `CallerDep` too; FastAPI caches the dependency, so it authenticates once.
AUTH = [Depends(authenticate)]

router = APIRouter(prefix="/v1", tags=["send"], dependencies=AUTH)


def _to_response(outcome: SendOutcome) -> SendEmailResponse:
    return SendEmailResponse(
        job_id=outcome.job_id,
        status=outcome.status,
        message_id=outcome.message_id,
        account=outcome.account_email,
        error_code=outcome.error_code,
        error_message=outcome.error_message,
    )


@router.post(
    "/send",
    response_model=SendEmailResponse,
    summary="Send one email (or queue it)",
)
def send(
    payload: SendEmailRequest,
    container: ContainerDep,
    caller: CallerDep,
) -> SendEmailResponse:
    """Validate, quota-check, and enqueue. The worker performs the Gmail call.

    `wait=true` (the default) blocks until the job finishes so a caller gets the Gmail message id
    directly; `wait=false` returns immediately with a job id, which is what a batch caller wants.
    """
    accounts = require(container, "accounts")
    resolved = accounts.resolve(payload.account)
    caller.require_account(resolved.email)
    outcome = require(container, "sender").send(
        account_email=resolved.email,
        msg=to_outgoing_message(payload, resolved.email, settings=container.settings),
        source="api",
        api_key=caller,
        idempotency_key=payload.idempotency_key,
        request_hash=(request_fingerprint(payload) if payload.idempotency_key else None),
        wait=payload.wait,
        thread_id=payload.thread_id,
    )
    return _to_response(outcome)


@router.post(
    "/send/batch",
    response_model=BatchSendResponse,
    summary="Queue up to 50 emails in one request",
)
def send_batch(
    payload: BatchSendRequest,
    container: ContainerDep,
    caller: CallerDep,
) -> BatchSendResponse:
    """The whole batch is pre-flighted first, so a refusal leaves nothing queued.

    Pacing is applied by the queue: the jobs are scheduled `send_interval_seconds` apart rather
    than sent back to back. `wait` applies to the batch as a whole - see `BatchSendRequest`.
    """
    accounts = require(container, "accounts")
    resolved = accounts.resolve(payload.account)
    caller.require_account(resolved.email)
    outcome: BatchOutcome = require(container, "sender").send_batch(
        account_email=resolved.email,
        messages=[
            to_outgoing_message(item, resolved.email, settings=container.settings)
            for item in payload.emails
        ],
        source="api",
        api_key=caller,
        wait=payload.wait,
    )
    return BatchSendResponse(jobs=[_to_response(item) for item in outcome.outcomes])


@router.get(
    "/send/limits",
    summary="Effective limits and pacing for the resolved account",
)
def send_limits(
    container: ContainerDep,
    account: str | None = Query(default=None),
) -> dict[str, object]:
    """What this gateway will actually enforce, as opposed to Gmail's published caps."""
    resolved = require(container, "accounts").resolve(account)
    settings = container.settings
    return {
        "account": resolved.email,
        "soft_limit_ratio": resolved.soft_limit_ratio,
        "daily_message_limit": resolved.daily_message_limit,
        "daily_recipient_limit": resolved.daily_recipient_limit,
        "send_interval_seconds": resolved.send_interval_seconds,
        "max_recipients_per_message": settings.max_recipients_per_message,
        "max_body_bytes": settings.max_body_bytes,
        "window_hours": 24.0,
    }


__all__ = ["router"]
