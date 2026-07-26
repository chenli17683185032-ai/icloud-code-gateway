from __future__ import annotations

from icloud_gateway.rate_limit import SlidingWindowRateLimiter


def test_sliding_window_blocks_until_oldest_event_expires() -> None:
    now = [100.0]
    limiter = SlidingWindowRateLimiter(clock=lambda: now[0])

    assert limiter.check("bucket", "key", limit=2, window_seconds=10).allowed
    assert limiter.check("bucket", "key", limit=2, window_seconds=10).allowed

    blocked = limiter.check("bucket", "key", limit=2, window_seconds=10)
    assert blocked.allowed is False
    assert blocked.retry_after == 10

    now[0] = 110.01
    assert limiter.check("bucket", "key", limit=2, window_seconds=10).allowed


def test_rate_limit_dimensions_are_independent() -> None:
    limiter = SlidingWindowRateLimiter(clock=lambda: 100.0)

    assert limiter.check("ip", "same", limit=1, window_seconds=60).allowed
    assert limiter.check("key", "same", limit=1, window_seconds=60).allowed
    assert limiter.check("ip", "other", limit=1, window_seconds=60).allowed
