"""Per-user quota and rate limit for the paid social provider (in-process, injectable clock)."""

import threading
import time
from collections import defaultdict, deque
from collections.abc import Callable


class QuotaExceeded(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class SocialQuota:
    def __init__(
        self,
        per_hour: int,
        per_day: int,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.per_hour, self.per_day, self.clock = per_hour, per_day, clock
        self._calls: dict[int, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def acquire(self, user_id: int | None) -> None:
        """Count one provider call or raise ``QuotaExceeded`` (caller falls back)."""
        key = user_id if user_id is not None else 0
        now = self.clock()
        with self._lock:
            calls = self._calls[key]
            while calls and now - calls[0] >= 86400:
                calls.popleft()
            if len(calls) >= self.per_day:
                raise QuotaExceeded("SOCIAL_DAILY_QUOTA_EXCEEDED")
            if sum(1 for t in calls if now - t < 3600) >= self.per_hour:
                raise QuotaExceeded("SOCIAL_HOURLY_RATE_LIMITED")
            calls.append(now)
