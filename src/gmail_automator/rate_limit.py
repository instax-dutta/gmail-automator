"""Per-key request rate limiting (PRD 5.3).

Gmail's limits protect the account; this protects the *gateway* from one noisy client filling the
queue or starving the others. The limiter is in-process and deliberately simple: a fixed window per
key, which is enough to stop a runaway agent and costs nothing to reason about. Behind more than
one gateway process, put a shared limiter in front (see docs/operations.md).
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

from gmail_automator.clock import Clock, SystemClock
from gmail_automator.errors import QuotaExceeded


@dataclass
class _Window:
    started_at: float
    count: int = 0


@dataclass
class KeyRateLimiter:
    """Fixed-window per-key limiter, keyed by API key id (`None` for the local identity)."""

    clock: Clock = field(default_factory=SystemClock)
    _windows: dict[int | None, _Window] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def check(self, key_id: int | None, limit_per_minute: int | None) -> None:
        """Raise `QuotaExceeded` when this key has already used its budget this minute.

        `limit_per_minute is None` means unlimited, which is the default for a key.
        """
        if not limit_per_minute or limit_per_minute <= 0:
            return
        now = self._epoch()
        with self._lock:
            window = self._windows.get(key_id)
            if window is None or now - window.started_at >= 60.0:
                window = _Window(started_at=now)
                self._windows[key_id] = window
            if window.count >= limit_per_minute:
                retry_after = max(0.0, 60.0 - (now - window.started_at))
                raise QuotaExceeded(
                    f"API key rate limit of {limit_per_minute} requests/minute reached",
                    details={
                        "resource": "api_key_rate",
                        "key_id": key_id,
                        "limit_per_minute": limit_per_minute,
                        "retry_after_seconds": round(retry_after, 3),
                    },
                )
            window.count += 1

    def reset(self, key_id: int | None = None) -> None:
        with self._lock:
            if key_id is None:
                self._windows.clear()
            else:
                self._windows.pop(key_id, None)

    def used(self, key_id: int | None) -> int:
        with self._lock:
            window = self._windows.get(key_id)
            return window.count if window else 0

    def _epoch(self) -> float:
        return self.clock.now().timestamp()


__all__ = ["KeyRateLimiter"]
