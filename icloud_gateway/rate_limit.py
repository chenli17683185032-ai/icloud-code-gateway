from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    retry_after: int = 0


class SlidingWindowRateLimiter:
    def __init__(self, *, clock=time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.RLock()
        self._events: dict[tuple[str, str], deque[float]] = defaultdict(deque)

    def check(
        self,
        bucket: str,
        key: str,
        *,
        limit: int,
        window_seconds: float,
    ) -> RateLimitDecision:
        bounded_limit = max(1, int(limit))
        bounded_window = max(1.0, float(window_seconds))
        now = float(self._clock())
        identifier = (str(bucket), str(key))
        cutoff = now - bounded_window
        with self._lock:
            events = self._events[identifier]
            while events and events[0] <= cutoff:
                events.popleft()
            if len(events) >= bounded_limit:
                retry_after = max(1, int(events[0] + bounded_window - now + 0.999))
                return RateLimitDecision(allowed=False, retry_after=retry_after)
            events.append(now)
            self._purge_empty(now, bounded_window)
        return RateLimitDecision(allowed=True)

    def _purge_empty(self, now: float, active_window: float) -> None:
        if len(self._events) < 1_000:
            return
        cutoff = now - max(active_window, 3_600.0)
        stale = [
            identifier
            for identifier, events in self._events.items()
            if not events or events[-1] <= cutoff
        ]
        for identifier in stale:
            self._events.pop(identifier, None)


__all__ = ["RateLimitDecision", "SlidingWindowRateLimiter"]
