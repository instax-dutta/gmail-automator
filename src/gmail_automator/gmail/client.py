"""Gmail transport: the only module in the codebase allowed to touch ``googleapiclient``.

Everything above this seam sees a `GmailTransport` and a small, fully-typed result object, which
is what makes the rest of the gateway testable without a network. Error classification lives here
too: the worker and the retry policy must not have to understand Google's error envelope.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

import httplib2
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

#: Reasons that will not improve by retrying. Per Google's own documentation a daily-quota
#: rejection can persist for hours, so hot-retrying it only burns quota and delays recovery (R3).
TERMINAL_REASONS: frozenset[str] = frozenset(
    {
        "dailySendQuotaExceeded",
        "sendDenied",
        "mailNotSent",
        "insufficientPermissions",
        "forbidden",
        "invalid",
        "invalidArgument",
        "backendErrorInvalidToken",
    }
)

#: Reasons that are explicitly transient.
RETRYABLE_REASONS: frozenset[str] = frozenset(
    {
        "rateLimitExceeded",
        "userRateLimitExceeded",
        "backendError",
        "internalFailure",
        "serviceUnavailable",
        "quotaExceeded",
    }
)


@dataclass(frozen=True)
class SendResult:
    message_id: str
    thread_id: str | None
    label_ids: tuple[str, ...]


@dataclass(frozen=True)
class MessageSummary:
    """One message as a list result: enough to decide whether to read it, not the body."""

    id: str
    thread_id: str | None
    subject: str
    sender: str
    recipients: str
    date: str
    snippet: str
    label_ids: tuple[str, ...]


@dataclass(frozen=True)
class MessagePage:
    messages: tuple[MessageSummary, ...]
    next_page_token: str | None
    result_size_estimate: int


@dataclass(frozen=True)
class MessageDetail(MessageSummary):
    """A message with its body and the threading headers a reply needs."""

    message_id_header: str | None
    in_reply_to: str | None
    references: str | None
    body_text: str | None
    body_html: str | None


@dataclass(frozen=True)
class LabelInfo:
    """One label, with the counts that let an agent decide whether it is worth listing."""

    id: str
    name: str
    type: str
    messages_total: int
    messages_unread: int


@dataclass(frozen=True)
class LabelResult:
    id: str
    label_ids: tuple[str, ...]


@dataclass(frozen=True)
class DraftResult:
    draft_id: str
    message_id: str | None
    thread_id: str | None


class GoogleApiError(Exception):
    """A normalized Gmail API failure."""

    def __init__(
        self,
        *,
        status_code: int | None,
        reason: str | None,
        message: str,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.reason = reason
        self.message = message
        self.retry_after = retry_after

    @property
    def retryable(self) -> bool:
        if self.reason in TERMINAL_REASONS:
            return False
        if self.reason in RETRYABLE_REASONS:
            return True
        if self.status_code == 429:
            return True
        return self.status_code is not None and self.status_code >= 500

    def __repr__(self) -> str:
        return (
            f"GoogleApiError(status_code={self.status_code!r}, reason={self.reason!r}, "
            f"retryable={self.retryable!r})"
        )


class AuthExpired(GoogleApiError):
    """Gmail rejected the access token; refresh once and retry."""


class GmailTransport(Protocol):
    def send_raw(
        self, *, email: str, access_token: str, raw_b64url: str, thread_id: str | None = None
    ) -> SendResult: ...

    def create_draft(
        self, *, email: str, access_token: str, raw_b64url: str, thread_id: str | None = None
    ) -> DraftResult: ...

    def list_messages(
        self,
        *,
        email: str,
        access_token: str,
        query: str | None = None,
        max_results: int = 10,
        page_token: str | None = None,
    ) -> MessagePage: ...

    def get_message(
        self, *, email: str, access_token: str, message_id: str, headers: tuple[str, ...] = ()
    ) -> dict[str, Any]: ...

    def modify_message(
        self,
        *,
        email: str,
        access_token: str,
        message_id: str,
        add_label_ids: tuple[str, ...] = (),
        remove_label_ids: tuple[str, ...] = (),
    ) -> LabelResult: ...

    def list_labels(self, *, email: str, access_token: str) -> tuple[LabelInfo, ...]: ...


class GoogleGmailTransport:
    """`GmailTransport` backed by google-api-python-client."""

    def __init__(
        self,
        *,
        api_endpoint: str,
        timeout: float = 30.0,
        http: Any | None = None,
    ) -> None:
        self.api_endpoint = api_endpoint
        self.timeout = timeout
        self._http = http

    def _service(self, access_token: str) -> Any:
        http = self._http or httplib2.Http(timeout=self.timeout)
        return build(
            "gmail",
            "v1",
            http=http,
            client_options={"api_endpoint": self.api_endpoint},
            cache_discovery=False,
        )

    def send_raw(
        self, *, email: str, access_token: str, raw_b64url: str, thread_id: str | None = None
    ) -> SendResult:
        service = self._service(access_token)
        body: dict[str, Any] = {"raw": raw_b64url}
        if thread_id:
            body["threadId"] = thread_id
        request = service.users().messages().send(userId=email, body=body)
        # The bearer token is attached here rather than through AuthorizedHttp so the transport
        # keeps no credential object with a refresh path of its own: the worker owns refreshing.
        request.headers["authorization"] = f"Bearer {access_token}"
        try:
            payload = request.execute()
        except HttpError as exc:
            raise _translate(exc) from exc
        return SendResult(
            message_id=str(payload.get("id", "")),
            thread_id=payload.get("threadId"),
            label_ids=tuple(payload.get("labelIds") or ()),
        )

    def create_draft(
        self, *, email: str, access_token: str, raw_b64url: str, thread_id: str | None = None
    ) -> DraftResult:
        """Create a draft instead of sending. Requires the `gmail.compose` scope.

        The caller checks the scope first, so a 403 here means Google changed its mind, not that the
        request was misconfigured.
        """
        service = self._service(access_token)
        message: dict[str, Any] = {"raw": raw_b64url}
        if thread_id:
            message["threadId"] = thread_id
        request = service.users().drafts().create(userId=email, body={"message": message})
        request.headers["authorization"] = f"Bearer {access_token}"
        try:
            payload = request.execute()
        except HttpError as exc:
            raise _translate(exc) from exc
        inner = payload.get("message") or {}
        return DraftResult(
            draft_id=str(payload.get("id", "")),
            message_id=inner.get("id"),
            thread_id=inner.get("threadId") or payload.get("threadId"),
        )

    # ------------------------------------------------------------------ mailbox

    def list_messages(
        self,
        *,
        email: str,
        access_token: str,
        query: str | None = None,
        max_results: int = 10,
        page_token: str | None = None,
    ) -> MessagePage:
        """Search the mailbox with Gmail's own query syntax.

        The query is passed through rather than reimplemented: Gmail's `q` already supports
        `from:`, `subject:`, `newer_than:`, `has:attachment`, `is:unread`, and boolean
        operators, and a second dialect to learn would be a worse version of the same thing.
        """
        service = self._service(access_token)
        params: dict[str, Any] = {"userId": email, "maxResults": max(1, min(max_results, 100))}
        if query:
            params["q"] = query
        if page_token:
            params["pageToken"] = page_token
        request = service.users().messages().list(**params)
        request.headers["authorization"] = f"Bearer {access_token}"
        try:
            payload = request.execute()
        except HttpError as exc:
            raise _translate(exc) from exc

        def hydrate(item: dict[str, Any]) -> MessageSummary:
            """Fill in the envelope for a list row, because `messages.list` does not return one.

            A list of ids with no subject and no sender is not something an agent can act on: it
            would have to fetch every message to learn which one it wanted. Gmail has no batch
            metadata endpoint, so this costs one `messages.get` per row, bounded by `max_results`
            (capped at 100 for exactly this reason). A row whose fetch fails keeps its id and an
            empty envelope rather than failing the page, so one unreadable message cannot make an
            otherwise good search useless.
            """
            summary = _summary_from_list_item(item)
            if not summary.id:
                return summary
            try:
                detail = self.get_message(
                    email=email,
                    access_token=access_token,
                    message_id=summary.id,
                    headers=_ENVELOPE_HEADERS,
                )
            except GoogleApiError:
                return summary
            headers = {
                str(entry.get("name", "")).lower(): entry.get("value")
                for entry in (detail.get("payload") or {}).get("headers") or ()
            }
            return MessageSummary(
                id=summary.id,
                thread_id=summary.thread_id,
                subject=str(headers.get("subject") or ""),
                sender=str(headers.get("from") or ""),
                recipients=str(headers.get("to") or ""),
                date=str(headers.get("date") or ""),
                snippet=summary.snippet or str((detail.get("payload") or {}).get("snippet") or ""),
                label_ids=summary.label_ids,
            )

        return MessagePage(
            messages=tuple(hydrate(item) for item in payload.get("messages") or ()),
            next_page_token=payload.get("nextPageToken"),
            result_size_estimate=int(payload.get("resultSizeEstimate") or 0),
        )

    def get_message(
        self,
        *,
        email: str,
        access_token: str,
        message_id: str,
        headers: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        """Fetch one message. Returns the raw Gmail payload; body parsing belongs to the service.

        Requested headers come back under `payload.headers` alongside Gmail's own envelope, so the
        caller never has to guess which of the two holds the RFC Message-ID.
        """
        service = self._service(access_token)
        params: dict[str, Any] = {"userId": email, "id": message_id, "format": "full"}
        if headers:
            params["metadataHeaders"] = list(headers)
        request = service.users().messages().get(**params)
        request.headers["authorization"] = f"Bearer {access_token}"
        try:
            return dict(request.execute())
        except HttpError as exc:
            raise _translate(exc) from exc

    def list_labels(self, *, email: str, access_token: str) -> tuple[LabelInfo, ...]:
        """Every label on the account, system and custom.

        An agent asked to file a message needs to know what the person actually named their
        labels; inventing a label silently creates a new one in Gmail.
        """
        service = self._service(access_token)
        request = service.users().labels().list(userId=email)
        request.headers["authorization"] = f"Bearer {access_token}"
        try:
            payload = request.execute()
        except HttpError as exc:
            raise _translate(exc) from exc
        return tuple(
            LabelInfo(
                id=str(item.get("id", "")),
                name=str(item.get("name", "")),
                type=str(item.get("type", "")),
                messages_total=int(item.get("messagesTotal") or 0),
                messages_unread=int(item.get("messagesUnread") or 0),
            )
            for item in payload.get("labels") or ()
        )

    def modify_message(
        self,
        *,
        email: str,
        access_token: str,
        message_id: str,
        add_label_ids: tuple[str, ...] = (),
        remove_label_ids: tuple[str, ...] = (),
    ) -> LabelResult:
        """Add and remove labels. This is how read/unread, star, archive, and trash are expressed.

        `trash` rather than `delete`: `gmail.modify` does not grant permanent deletion, and a
        gateway that can silently destroy mail is not a tool an agent should hold.
        """
        service = self._service(access_token)
        body: dict[str, Any] = {}
        if add_label_ids:
            body["addLabelIds"] = list(add_label_ids)
        if remove_label_ids:
            body["removeLabelIds"] = list(remove_label_ids)
        if not body:
            raise ValueError("modify_message needs at least one label to add or remove")
        request = service.users().messages().modify(userId=email, id=message_id, body=body)
        request.headers["authorization"] = f"Bearer {access_token}"
        try:
            payload = request.execute()
        except HttpError as exc:
            raise _translate(exc) from exc
        return LabelResult(
            id=str(payload.get("id", message_id)),
            label_ids=tuple(payload.get("labelIds") or ()),
        )


#: What a list row needs to be useful: who and what, not the body.
_ENVELOPE_HEADERS: tuple[str, ...] = ("Subject", "From", "To", "Date")


def _summary_from_list_item(item: dict[str, Any]) -> MessageSummary:
    """Build a list-row summary from Gmail's `messages.list` item.

    The list endpoint returns only ids, a thread, a snippet, and label ids. Empty strings keep one
    shape for the agent instead of two, and a list row should never invent a sender it was not
    told. `list_messages` fills these in with a follow-up metadata call per row.
    """
    return MessageSummary(
        id=str(item.get("id", "")),
        thread_id=item.get("threadId"),
        subject="",
        sender="",
        recipients="",
        date="",
        snippet=str(item.get("snippet", "") or ""),
        label_ids=tuple(item.get("labelIds") or ()),
    )


def _translate(exc: HttpError) -> GoogleApiError:
    status = exc.resp.status if exc.resp is not None else None
    reason, message = _parse_error_body(exc.content)
    retry_after = _parse_retry_after(exc.resp)
    error_type = AuthExpired if status == 401 else GoogleApiError
    return error_type(status_code=status, reason=reason, message=message, retry_after=retry_after)


def _parse_error_body(content: str | bytes | None) -> tuple[str | None, str]:
    import json

    if not content:
        return None, "Gmail API request failed"
    text = content.decode("utf-8", "replace") if isinstance(content, bytes) else content
    try:
        body = json.loads(text)
    except ValueError:
        return None, text[:500]
    if not isinstance(body, dict):
        return None, text[:500]
    error = body.get("error")
    if not isinstance(error, dict):
        return None, str(error or body)[:500]
    reason: str | None = None
    for entry in error.get("errors") or []:
        if isinstance(entry, dict) and entry.get("reason"):
            reason = str(entry["reason"])
            break
    message = str(error.get("message") or error.get("status") or "Gmail API request failed")
    return reason, message[:500]


def _parse_retry_after(resp: Any) -> float | None:
    if resp is None:
        return None
    headers = getattr(resp, "headers", None) or {}
    raw = headers.get("retry-after") if hasattr(headers, "get") else None
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None
