"""``AsyncRateLimiter``'s pacing, ``Rate``'s units, and the limiter a ``PathLimiters`` rule selects."""

from __future__ import annotations

import asyncio
import time

import httpx2
import pytest

from headgate._rate_limit import UNLIMITED, AsyncRateLimiter, PathLimiters, Rate, RateRule


@pytest.mark.transport_policy  # the subject is the throttle's timing: keep the real wait (see no_rate_limit)
class TestAsyncRateLimiter:
    async def test_first_call_no_delay(self) -> None:
        # The first call is admitted at once, whatever the rate.
        limiter = AsyncRateLimiter(Rate.per_second(2))
        start = time.monotonic()
        await limiter.wait()
        elapsed = time.monotonic() - start
        assert elapsed < 0.1

    async def test_second_call_respects_rate(self) -> None:
        # 1 req/s: the second immediate call waits ~1s.
        limiter = AsyncRateLimiter(Rate.per_second(1))
        await limiter.wait()
        start = time.monotonic()
        await limiter.wait()
        elapsed = time.monotonic() - start
        assert elapsed >= 0.5  # at least half a second with 1 req/s

    async def test_high_rate_no_delay(self) -> None:
        # 100 req/s: two consecutive calls are near-instant.
        limiter = AsyncRateLimiter(Rate.per_second(100))
        await limiter.wait()
        start = time.monotonic()
        await limiter.wait()
        elapsed = time.monotonic() - start
        assert elapsed < 0.1

    async def test_concurrent_calls_serialized_by_rate(self) -> None:
        # 5 req/s: a burst of 5 is admitted at once, and the 6th waits.
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
        # The 6th waits ~0.2s.
        assert elapsed >= 0.1

    async def test_sub_one_max_rate_preserved_not_clamped(self) -> None:
        # 0.5 req/s means one request per 2s, not a silent clamp up to 1 req/s.
        limiter = AsyncRateLimiter(Rate.per_second(0.5))
        await limiter.wait()
        start = time.monotonic()
        await limiter.wait()
        elapsed = time.monotonic() - start
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


def test_path_limiters_pick_the_first_matching_rule_and_default_otherwise() -> None:
    # Two rules whose fragments BOTH occur in /api/candles: "first match wins" is only observable when more than one
    # rule can match, and the second is declared after the first so a last-match scan picks the other limiter.
    limiters = PathLimiters(
        default=Rate.per_second(1000),
        rules=[RateRule("/candles", Rate.per_second(1000)), RateRule("/api", Rate.per_second(1000))],
    )
    (_, candles), (_, api) = limiters._rules

    both = httpx2.Request("GET", "https://x/api/candles?g=1")
    assert limiters.for_request(both) is candles, "the first matching rule must win"
    assert limiters.for_request(both) is not api
    assert limiters.for_request(both) is not limiters.default

    only_the_second = httpx2.Request("GET", "https://x/api/ticker")
    assert limiters.for_request(only_the_second) is api, "a request only the later rule matches still reaches it"

    assert limiters.for_request(httpx2.Request("GET", "https://x/ticker")) is limiters.default
