"""Small thread-safe rolling-window quota for public demo actions (AI runs, MCP calls)."""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Callable


class HourlyQuota:
    """Allows at most `limit` acquisitions per rolling `window_seconds`."""

    def __init__(
        self,
        limit: int,
        window_seconds: float = 3600.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._limit = limit
        self._window = window_seconds
        self._clock = clock
        self._stamps: deque[float] = deque()
        self._lock = threading.Lock()

    def _prune(self, now: float) -> None:
        while self._stamps and now - self._stamps[0] >= self._window:
            self._stamps.popleft()

    def try_acquire(self) -> bool:
        with self._lock:
            now = self._clock()
            self._prune(now)
            if len(self._stamps) >= self._limit:
                return False
            self._stamps.append(now)
            return True

    def remaining(self) -> int:
        with self._lock:
            self._prune(self._clock())
            return max(0, self._limit - len(self._stamps))
