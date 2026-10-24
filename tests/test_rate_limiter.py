"""Tests for async rate limiter."""

import asyncio
import time

import pytest

from headgate._rate_limiter import UNLIMITED, AsyncRateLimiter, Rate


@pytest.mark.transport_policy  # the subject is the throttle's timing: keep the real wait (see no_rate_limit)
class TestAsyncRateLimiter:
    async def test_first_call_no_delay(self) -> None:
        # First call always returns immediately regardless of rate
        limiter = AsyncRateLimiter(Rate.per_second(2))
        start = time.monotonic()
        await limiter.wait()
        elapsed = time.monotonic() - start
        assert elapsed < 0.1

    async def test_second_call_respects_rate(self) -> None:
        # 1 req/s: the second immediate call must wait ~1s
        limiter = AsyncRateLimiter(Rate.per_second(1))
        await limiter.wait()
        start = time.monotonic()
        await limiter.wait()
        elapsed = time.monotonic() - start
        assert elapsed >= 0.5  # at least half a second with 1 req/s

    async def test_high_rate_no_delay(self) -> None:
        # 100 req/s: two consecutive calls should be near-instant
        limiter = AsyncRateLimiter(Rate.per_second(100))
        await limiter.wait()
        start = time.monotonic()
        await limiter.wait()
        elapsed = time.monotonic() - start
        assert elapsed < 0.1

    async def test_concurrent_calls_serialized_by_rate(self) -> None:
        # 5 req/s over 1s: burst of 5 but 6th waits
        limiter = AsyncRateLimiter(Rate.per_second(5))
        start = time.monotonic()
        await asyncio.gather(
            limiter.wait(),
            limiter.wait(),
            limiter.wait(),
            limiter.wait(),
            limiter.wait(),
            limiter.wait(),
        )
        elapsed = time.monotonic() - start
        # 6 calls at 5/s: at least 1 must wait ~0.2s
        assert elapsed >= 0.1

    async def test_sub_one_max_rate_preserved_not_clamped(self) -> None:
        # a Rate of 0.5/s means "1 request per 2 seconds" — must not silently
        # clamp up to 1 req/s. Two back-to-back calls should be spaced by
        # ~period/requests seconds.
        limiter = AsyncRateLimiter(Rate.per_second(0.5))
        await limiter.wait()
        start = time.monotonic()
        await limiter.wait()
        elapsed = time.monotonic() - start
        # 0.5 req/s = 1 req per 2s; second call must wait noticeably
        # longer than the 1 req/s case (~1s).
        assert elapsed >= 1.5, f"expected ~2s gap for 0.5 req/s, got {elapsed:.2f}s"


def test_rate_units_are_in_the_constructor() -> None:
    assert Rate.per_second(2) == Rate(requests=2, period=1.0)
    assert Rate.per_minute(120) == Rate(requests=120, period=60.0)


@pytest.mark.parametrize(("requests", "period"), [(0, 1.0), (-1, 1.0), (1, 0.0), (1, -60.0)])
def test_a_rate_must_be_positive(requests: float, period: float) -> None:
    with pytest.raises(ValueError, match="must be > 0"):
        Rate(requests=requests, period=period)


def test_a_sub_one_rate_scales_its_period_rather_than_clamping() -> None:
    limiter = AsyncRateLimiter(Rate(requests=0.5, period=1.0))
    assert (limiter._max_rate, limiter._time_period) == (1.0, 2.0)


def test_unlimited_is_a_rate() -> None:
    assert isinstance(UNLIMITED, Rate)
    assert UNLIMITED.requests == 1_000_000.0
