"""Sliding-window monotonic rate limiter."""

from __future__ import annotations

import time
from collections import deque
from typing import Callable


class SlidingWindowRateLimiter:
    """
    Per-session sliding window rate limiter backed by a monotonic clock.
    Supports injecting a custom clock function for deterministic unit testing.
    """

    def __init__(self, clock: Callable[[], float] | None = None) -> None:
        self._clock: Callable[[], float] = clock or time.monotonic
        self._windows: dict[str, deque[float]] = {}

    def _prune(self, session_id: str, now: float, window_seconds: float) -> deque[float]:
        bucket = self._windows.setdefault(session_id, deque())
        cutoff = now - window_seconds
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()
        return bucket

    def peek(
        self,
        session_id: str,
        max_requests: int,
        window_seconds: float,
    ) -> tuple[int, float]:
        """Return (remaining_requests_in_window, retry_after_seconds) without consuming."""
        now = self._clock()
        bucket = self._prune(session_id, now, window_seconds)
        used = len(bucket)
        remaining = max(0, max_requests - used)
        if used >= max_requests and bucket:
            retry_after = max(0.0, round((bucket[0] + window_seconds) - now, 3))
        else:
            retry_after = 0.0
        return remaining, retry_after

    def check_and_consume(
        self,
        session_id: str,
        max_requests: int,
        window_seconds: float,
    ) -> tuple[bool, int, float]:
        """
        Evaluate whether session_id is within the sliding window limit.

        Returns:
            (allowed, remaining_requests_in_window, retry_after_seconds)
        """
        now = self._clock()
        bucket = self._prune(session_id, now, window_seconds)
        if len(bucket) >= max_requests:
            retry_after = max(0.01, round((bucket[0] + window_seconds) - now, 3))
            return False, 0, retry_after

        bucket.append(now)
        remaining = max(0, max_requests - len(bucket))
        return True, remaining, 0.0

    def reset(self, session_id: str | None = None) -> None:
        """Clear rate-limit history for a session or all sessions."""
        if session_id is None:
            self._windows.clear()
        else:
            self._windows.pop(session_id, None)

