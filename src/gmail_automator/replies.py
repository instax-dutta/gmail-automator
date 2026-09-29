"""Replying, to something the gateway sent or to any message the account can read.

Replying correctly is a threading problem, not a sending problem. Three things must line up:

- `In-Reply-To` carries the RFC 822 Message-ID of the message being answered. Gmail's own
  `messages.send` response `id` is *not* that - it is an internal hex id - so a reply built from the
  stored send result alone would put a meaningless value in the header and thread in no client.
- `References` is the whole chain, not just the parent. Omitting it is why a long thread stops
  threading after the first reply in some clients.
- Gmail's `threadId` is separate from both, and is what makes the reply appear inside the thread in
  Gmail's own UI rather than as a new conversation.

So a reply reads the original's headers first, then hands a normal `OutgoingMessage` to the existing
send or draft path. Nothing here re-implements sending.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from gmail_automator.clock import Clock, SystemClock
from gmail_automator.container import Container, require
from gmail_automator.drafts import DraftService, has_compose_scope
from gmail_automator.errors import InvalidRequest, ScopeMissing
from gmail_automator.gmail.client import DraftResult
from gmail_automator.gmail.mime import OutgoingMessage
from gmail_automator.mailbox import MailboxService
from gmail_automator.send import SendOutcome, SendService

#: Subjects that already read as a reply must not grow a second `Re:` on every hop. Compared with
#: whitespace removed, so `Re : x` and `RE:x` are both recognised.
_REPLY_PREFIXES = ("re:", "fw:", "fwd:")


def reply_subject(subject: str | None) -> str:
    """Prefix `Re:` unless the subject is already a reply, forwarding, or empty."""
    text = (subject or "").strip()
    if not text:
        return "Re:"
    # Folding can leave the prefix with odd internal whitespace ("Re : x"), so normalise the
    # comparison rather than only checking the literal lowercase form. Whitespace is allowed
    # between the letters and the colon, so it is removed before the comparison, not just lowered.
    normalised = re.sub(r"\s+", "", text).lower()
    if any(normalised.startswith(prefix) for prefix in _REPLY_PREFIXES):
        return text
    return f"Re: {text}"


def extend_references(existing: str | None, message_id: str | None) -> str | None:
    """Append the new message id to the chain, keeping whatever was already there.

    Duplicates are dropped: a client that re-sends headers can produce a chain with the same id
    twice, and a repeated `References` entry is at best noise and at worst a loop for some clients.
    """
    if not message_id:
        return existing
    seen: list[str] = []
    for token in (existing or "").split():
        if token and token not in seen:
            seen.append(token)
    if message_id not in seen:
        seen.append(message_id)
    return " ".join(seen) if seen else None


def reply_to(original: Any, *, account_email: str) -> OutgoingMessage:
    """Build the `OutgoingMessage` for a reply to `original`.

    `original` is a `MessageDetail`. The recipient defaults to the original's sender so an agent
    does not have to parse `From: Name <addr@x>` itself, and an explicit recipient always wins.
    """
    sender = extract_address(original.sender) or original.sender
    if not sender:
        raise InvalidRequest(
            "the original message has no usable From address to reply to",
            details={"message_id": original.id},
        )
    return OutgoingMessage(
        from_email=account_email,
        to=(sender,),
        subject=reply_subject(original.subject),
        body="",
        in_reply_to=original.message_id_header,
        references=extend_references(original.references, original.message_id_header),
    )


def extract_address(header: str | None) -> str:
    """Pull the bare address out of a `From: Name <addr@x>` header.

    Returns the input unchanged when it has no angle brackets, so a bare address passes through.
    """
    text = (header or "").strip()
    if "<" in text and ">" in text:
        inner = text[text.index("<") + 1 : text.index(">")]
        return inner.strip()
    return text


@dataclass(frozen=True)
class _Resolved:
    detail: Any
    thread_id: str | None


@dataclass(frozen=True)
class ReplyOutcome:
    """What a reply produced, in one shape for both adapters.

    Adapters used to branch on `hasattr(outcome, "draft_id")`, which puts a type check in two HTTP
    layers and makes "was this a draft?" unanswerable from the result. One dataclass states it
    explicitly, and carries the thread id so an agent can follow a reply up.
    """

    kind: str  # "sent" or "draft"
    account: str
    replied_to: str
    thread_id: str | None
    outcome: SendOutcome | DraftResult

    @property
    def _sent(self) -> SendOutcome | None:
        return self.outcome if isinstance(self.outcome, SendOutcome) else None

    @property
    def _draft(self) -> DraftResult | None:
        return self.outcome if isinstance(self.outcome, DraftResult) else None

    @property
    def job_id(self) -> int | None:
        sent = self._sent
        return sent.job_id if sent else None

    @property
    def message_id(self) -> str | None:
        return self.outcome.message_id

    @property
    def status(self) -> str | None:
        sent = self._sent
        return sent.status if sent else "draft"

    @property
    def draft_id(self) -> str | None:
        draft = self._draft
        return draft.draft_id if draft else None

    @property
    def error_code(self) -> str | None:
        sent = self._sent
        return sent.error_code if sent else None

    @property
    def error_message(self) -> str | None:
        sent = self._sent
        return sent.error_message if sent else None


class ReplyService:
    """Resolve a reply target, then route it through the normal send or draft path."""

    def __init__(self, *, container: Container) -> None:
        self._container = container

    @property
    def clock(self) -> Clock:
        return self._container.clock or SystemClock()

    def _mailbox(self) -> MailboxService:
        mailbox: MailboxService = require(self._container, "mailbox")
        return mailbox

    def _resolve_job(self, job_id: int, account_email: str | None) -> _Resolved:
        """Resolve a job the gateway sent into the Gmail message to reply to.

        This is the path that needs no read scope of the mailbox at all: the message id and thread
        id were recorded when the job was sent, so replying to your own outbound mail works on a
        send-only account.
        """
        session_factory = require(self._container, "session_factory")
        from sqlalchemy import select

        from gmail_automator.models import SendJob

        with session_factory() as session:
            job = session.scalar(select(SendJob).where(SendJob.id == job_id))
            if job is None:
                raise InvalidRequest(f"no send job {job_id}", details={"job_id": job_id})
            if job.status != "sent" or not job.gmail_message_id:
                raise InvalidRequest(
                    f"job {job_id} is {job.status}, so there is nothing to reply to",
                    details={"job_id": job_id, "status": job.status},
                )
            account = job.account.email
            message_id = job.gmail_message_id
            thread_id = job.gmail_thread_id
        if account_email and account.lower() != account_email.lower():
            raise InvalidRequest(
                f"job {job_id} was sent from {account}, not {account_email}",
                details={"job_id": job_id, "account": account},
            )
        detail = self._mailbox().get_message(message_id=message_id, account_email=account)
        return _Resolved(detail=detail, thread_id=thread_id or detail.thread_id)

    def _resolve_message(self, message_id: str, account_email: str | None) -> _Resolved:
        detail = self._mailbox().get_message(message_id=message_id, account_email=account_email)
        return _Resolved(detail=detail, thread_id=detail.thread_id)

    def reply(
        self,
        *,
        body: str,
        account_email: str | None = None,
        job_id: int | None = None,
        message_id: str | None = None,
        to: tuple[str, ...] | None = None,
        cc: tuple[str, ...] | None = None,
        bcc: tuple[str, ...] | None = None,
        body_html: str | None = None,
        subject: str | None = None,
        draft: bool = False,
        wait: bool = True,
        now: datetime | None = None,
    ) -> ReplyOutcome:
        """Reply to a message, or draft the reply for review.

        Exactly one of `job_id` and `message_id` identifies the target. `job_id` addresses a message
        this gateway sent; `message_id` addresses any message the account can read.
        """
        if (job_id is None) == (message_id is None):
            raise InvalidRequest(
                "give exactly one of job_id (a message this gateway sent) or message_id "
                "(any message the account can read)"
            )
        now = now or self.clock.now()
        account = require(self._container, "accounts").resolve(account_email)

        resolved = (
            self._resolve_job(job_id, account.email)
            if job_id is not None
            else self._resolve_message(str(message_id), account.email)
        )
        original = resolved.detail

        # Read scope is not checked here: resolving the target already read the original, and
        # `MailboxService` owns that check. Re-checking would duplicate the rule and the message.
        if draft and not has_compose_scope(account):
            raise ScopeMissing(
                "drafting a reply needs a compose scope; "
                f"{account.email} was connected with "
                f"{', '.join(sorted(account.scopes or [])) or 'no scopes'}",
                details={
                    "account": account.email,
                    "required_scopes": ["https://www.googleapis.com/auth/gmail.modify"],
                    "granted_scopes": account.scopes or [],
                },
            )

        base = reply_to(original, account_email=account.email)
        recipients = to if to else base.to
        message = OutgoingMessage(
            from_email=account.email,
            to=tuple(recipients),
            cc=cc or (),
            bcc=bcc or (),
            subject=subject or base.subject,
            body=body,
            body_html=body_html,
            in_reply_to=base.in_reply_to,
            references=base.references,
        )

        thread_id = resolved.thread_id
        target = f"job:{job_id}" if job_id is not None else f"message:{message_id}"
        if draft:
            drafts: DraftService = require(self._container, "drafts")
            created = drafts.create_draft(
                account_email=account.email,
                msg=message,
                now=now,
                thread_id=thread_id,
            )
            return ReplyOutcome(
                kind="draft",
                account=account.email,
                replied_to=target,
                thread_id=created.thread_id or thread_id,
                outcome=created,
            )
        send: SendService = require(self._container, "sender")
        sent = send.send(
            account_email=account.email,
            msg=message,
            source="mcp_reply",
            wait=wait,
            now=now,
            thread_id=thread_id,
        )
        # A queued reply has no thread id yet: Gmail assigns it when the worker sends. Reporting the
        # thread we threaded into is still correct and is what a follow-up needs.
        return ReplyOutcome(
            kind="sent",
            account=account.email,
            replied_to=target,
            thread_id=thread_id,
            outcome=sent,
        )


__all__ = [
    "ReplyOutcome",
    "ReplyService",
    "extend_references",
    "extract_address",
    "reply_subject",
    "reply_to",
]
