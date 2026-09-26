from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any


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

    def script_result(self, message_id: str, thread_id: str | None = None) -> None:
        from fmaiily.gmail.client import SendResult

        self._results.append(
            SendResult(message_id=message_id, thread_id=thread_id, label_ids=("SENT",))
        )

    def script_error(self, error: Exception) -> None:
        self._errors.append(error)

    def script_draft_result(
        self, draft_id: str, message_id: str | None = None, thread_id: str | None = None
    ) -> None:
        from fmaiily.gmail.client import DraftResult

        self._draft_results.append(
            DraftResult(draft_id=draft_id, message_id=message_id, thread_id=thread_id)
        )

    def script_draft_error(self, error: Exception) -> None:
        self._draft_errors.append(error)

    def create_draft(
        self, *, email: str, access_token: str, raw_b64url: str, thread_id: str | None = None
    ) -> Any:
        from fmaiily.gmail.client import DraftResult

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
        from fmaiily.gmail.client import SendResult

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
