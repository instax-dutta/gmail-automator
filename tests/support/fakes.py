from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from gmail_automator.gmail.client import GoogleApiError
from tests.support.gmail_payload import gmail_payload


class FakeClock:
    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 9, 26, 12, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self._now

    def set(self, value: datetime) -> None:
        self._now = value

    def advance(self, delta: timedelta) -> None:
        self._now += delta


class RecordingSleeper:
    def __init__(self, clock: FakeClock | None = None) -> None:
        self.clock = clock
        self.slept: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        if self.clock is not None:
            self.clock.advance(timedelta(seconds=seconds))


@dataclass
class FakeGmailTransport:
    """Scriptable stand-in for GmailTransport."""

    default_message_id: str = "msg-1"
    calls: list[dict[str, Any]] = field(default_factory=list)
    draft_calls: list[dict[str, Any]] = field(default_factory=list)
    _results: list[Any] = field(default_factory=list)
    _errors: list[Exception] = field(default_factory=list)
    _draft_results: list[Any] = field(default_factory=list)
    _draft_errors: list[Exception] = field(default_factory=list)
    #: Seeded messages for the read/organise surface, keyed by id.
    messages: dict[str, Any] = field(default_factory=dict)
    #: Next page token handed back by list_messages when set, to exercise pagination.
    next_page_token: str | None = None
    labels: tuple[Any, ...] = ()

    def script_result(self, message_id: str, thread_id: str | None = None) -> None:
        from gmail_automator.gmail.client import SendResult

        self._results.append(
            SendResult(message_id=message_id, thread_id=thread_id, label_ids=("SENT",))
        )

    def script_error(self, error: Exception) -> None:
        self._errors.append(error)

    def script_draft_result(
        self, draft_id: str, message_id: str | None = None, thread_id: str | None = None
    ) -> None:
        from gmail_automator.gmail.client import DraftResult

        self._draft_results.append(
            DraftResult(draft_id=draft_id, message_id=message_id, thread_id=thread_id)
        )

    def script_draft_error(self, error: Exception) -> None:
        self._draft_errors.append(error)

    def seed_message(
        self,
        message_id: str,
        *,
        thread_id: str | None = "thread-1",
        subject: str = "Original subject",
        sender: str = "Someone <someone@example.com>",
        to: str = "me@example.com",
        message_id_header: str | None = None,
        references: str | None = None,
        in_reply_to: str | None = None,
        body: str = "the original body",
        raw: bytes | None = None,
        label_ids: tuple[str, ...] = ("INBOX", "UNREAD"),
        snippet: str = "snippet",
    ) -> str:
        """Add a message the read/organise surface can return.

        The default is a single-part `text/plain` message. Pass `raw` to seed any other shape -
        an HTML-only `multipart/alternative` from Exchange, say - because a double that can only
        produce one shape hides the parser bugs that live in the others.
        """
        message_id_header = message_id_header or f"<{message_id}@example.com>"
        # The raw MIME is assembled here rather than through `build_mime`, because a seeded message
        # needs a chosen Message-ID and the builder generates its own. The wire format is the same,
        # so the parsing path under test is the parsing path production uses.
        headers: dict[str, str] | None = None
        if raw is None:
            raw = (
                f"Message-ID: {message_id_header}\r\n"
                f"From: {sender}\r\n"
                f"To: {to}\r\n"
                f"Subject: {subject}\r\n"
                "Date: Tue, 29 Sep 2026 12:00:00 +0000\r\n"
                + (f"In-Reply-To: {in_reply_to}\r\n" if in_reply_to else "")
                + (f"References: {references}\r\n" if references else "")
                + "Content-Type: text/plain; charset=utf-8\r\n"
                "\r\n"
                f"{body}\r\n"
            ).encode("utf-8")
            headers = {
                "Message-ID": message_id_header,
                "From": sender,
                "To": to,
                "Subject": subject,
                "Date": "Tue, 29 Sep 2026 12:00:00 +0000",
                **({"References": references} if references else {}),
                **({"In-Reply-To": in_reply_to} if in_reply_to else {}),
            }
        # A message seeded with its own `raw` carries that message's own headers, rather than the
        # defaults, so the envelope and the body cannot disagree about who sent what.
        self.messages[message_id] = {
            "id": message_id,
            "threadId": thread_id,
            "snippet": snippet,
            "labelIds": list(label_ids),
            "headers": headers,
            "raw": raw,
        }
        return message_id

    def list_messages(
        self,
        *,
        email: str,
        access_token: str,
        query: str | None = None,
        max_results: int = 10,
        page_token: str | None = None,
    ) -> Any:
        from gmail_automator.gmail.client import MessagePage, MessageSummary

        self.calls.append({"op": "list_messages", "query": query, "max_results": max_results})
        summaries = tuple(
            MessageSummary(
                id=item["id"],
                thread_id=item.get("threadId"),
                subject="",
                sender="",
                recipients="",
                date="",
                snippet=item.get("snippet", ""),
                label_ids=tuple(item.get("labelIds", [])),
            )
            for item in self.messages.values()
        )
        return MessagePage(
            messages=summaries,
            next_page_token=self.next_page_token,
            result_size_estimate=len(summaries),
        )

    def get_message(
        self, *, email: str, access_token: str, message_id: str, headers: tuple[str, ...] = ()
    ) -> Any:

        self.calls.append({"op": "get_message", "message_id": message_id})
        stored = self.messages.get(message_id)
        if stored is None:
            raise GoogleApiError(status_code=404, reason="notFound", message="message not found")
        return gmail_payload(
            stored.get("raw", b""),
            message_id=stored["id"],
            thread_id=stored.get("threadId"),
            label_ids=tuple(stored.get("labelIds", [])),
            snippet=stored.get("snippet", ""),
            headers=stored.get("headers"),
        )

    def modify_message(
        self,
        *,
        email: str,
        access_token: str,
        message_id: str,
        add_label_ids: tuple[str, ...] = (),
        remove_label_ids: tuple[str, ...] = (),
    ) -> Any:
        from gmail_automator.gmail.client import LabelResult

        self.calls.append(
            {
                "op": "modify_message",
                "message_id": message_id,
                "add": tuple(add_label_ids),
                "remove": tuple(remove_label_ids),
            }
        )
        stored = self.messages.get(message_id)
        if stored is None:
            raise GoogleApiError(status_code=404, reason="notFound", message="message not found")
        labels = set(stored.get("labelIds", []))
        labels.update(add_label_ids)
        labels.difference_update(remove_label_ids)
        stored["labelIds"] = sorted(labels)
        return LabelResult(id=message_id, label_ids=tuple(sorted(labels)))

    def list_labels(self, *, email: str, access_token: str) -> tuple[Any, ...]:
        self.calls.append({"op": "list_labels"})
        return self.labels

    def create_draft(
        self, *, email: str, access_token: str, raw_b64url: str, thread_id: str | None = None
    ) -> Any:
        from gmail_automator.gmail.client import DraftResult

        self.draft_calls.append(
            {
                "email": email,
                "access_token": access_token,
                "raw_b64url": raw_b64url,
                "thread_id": thread_id,
            }
        )
        if self._draft_errors:
            raise self._draft_errors.pop(0)
        if self._draft_results:
            return self._draft_results.pop(0)
        return DraftResult(draft_id="draft-1", message_id="draft-msg-1", thread_id=thread_id)

    def send_raw(
        self, *, email: str, access_token: str, raw_b64url: str, thread_id: str | None = None
    ) -> Any:
        from gmail_automator.gmail.client import SendResult

        self.calls.append(
            {
                "email": email,
                "access_token": access_token,
                "raw_b64url": raw_b64url,
                "thread_id": thread_id,
            }
        )
        if self._errors:
            raise self._errors.pop(0)
        if self._results:
            return self._results.pop(0)
        return SendResult(
            message_id=self.default_message_id, thread_id=thread_id, label_ids=("SENT",)
        )
