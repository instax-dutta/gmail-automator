from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...


class Sleeper(Protocol):
    def sleep(self, seconds: float) -> None: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class SystemSleeper:
    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)
