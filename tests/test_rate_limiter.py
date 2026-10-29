"""Unit tests for the monotonic sliding-window rate limiter."""

from __future__ import annotations

from server.services.rate_limiter import SlidingWindowRateLimiter
from tests.conftest import FakeClock


def test_sliding_window_rate_limiter_enforces_window_and_recovers() -> None:
    clock = FakeClock(initial=500.0)
    limiter = SlidingWindowRateLimiter(clock=clock)

    # Window is 3 requests per 10 seconds
    ok1, rem1, retry1 = limiter.check_and_consume("ses_1", 3, 10.0)
    assert ok1 is True and rem1 == 2 and retry1 == 0.0

    clock.advance(2.0)
    ok2, rem2, _ = limiter.check_and_consume("ses_1", 3, 10.0)
    assert ok2 is True and rem2 == 1

    clock.advance(2.0)
    ok3, rem3, _ = limiter.check_and_consume("ses_1", 3, 10.0)
    assert ok3 is True and rem3 == 0

    # 4th request at t=504.0 (within 10s of first request at t=500.0) must be blocked
    ok4, rem4, retry4 = limiter.check_and_consume("ses_1", 3, 10.0)
    assert ok4 is False
    assert rem4 == 0
    assert 5.9 <= retry4 <= 6.1

    # Different session is isolated
    ok_other, rem_other, _ = limiter.check_and_consume("ses_2", 3, 10.0)
    assert ok_other is True and rem_other == 2

    # Advance clock past the first request's 10s window (to t=510.1)
    clock.advance(6.1)
    ok5, rem5, retry5 = limiter.check_and_consume("ses_1", 3, 10.0)
    assert ok5 is True
    assert rem5 == 0
    assert retry5 == 0.0

