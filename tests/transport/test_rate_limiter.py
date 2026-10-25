# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""``AsyncRateLimiter``'s pacing, ``Rate``'s units, and the limiter a ``PathLimiters`` rule selects."""

from __future__ import annotations

import asyncio
import time
import warnings

import httpx2
import pytest

from headgate._rate_limit import UNLIMITED, AsyncRateLimiter, PathLimiters, Rate, RateRule
from headgate.policy import policy_disabled

#: Under this many seconds, a wait counts as no wait at all.
#: A period short enough that a real wait keeps the suite fast, long enough to dwarf timer slack.
_SHORT_PERIOD = 0.05
_NO_DELAY = 0.1

#: A burst size, and a rate whose burst it is.
_BURST = 5


async def _elapsed_for_second_wait(limiter: AsyncRateLimiter) -> float:
    """Take one slot, then return how long a second, immediate one waits."""
    await limiter.wait()
    start = time.monotonic()
    await limiter.wait()
    return time.monotonic() - start


@pytest.mark.transport_policy  # the subject is the throttle's timing: keep the real wait (see no_rate_limit)
class TestAsyncRateLimiter:
    """The limiter's real pacing, measured on the clock."""

    async def test_first_call_no_delay(self) -> None:
        """Admit the first call at once, whatever the rate."""
        limiter = AsyncRateLimiter(Rate.per_second(2))
        start = time.monotonic()
        await limiter.wait()
        assert time.monotonic() - start < _NO_DELAY

    async def test_second_call_respects_rate(self) -> None:
        """Make the second immediate call at one request per period wait about a period (at least half of one)."""
        rate = Rate(requests=1, period=_SHORT_PERIOD)
        assert await _elapsed_for_second_wait(AsyncRateLimiter(rate)) >= rate.period / 2

    async def test_high_rate_no_delay(self) -> None:
        """Admit two consecutive calls at 100 req/s near-instantly."""
        assert await _elapsed_for_second_wait(AsyncRateLimiter(Rate.per_second(100))) < _NO_DELAY

    async def test_concurrent_calls_serialized_by_rate(self) -> None:
        """Admit a burst of ``rate.requests`` at once, and make the next caller wait for the bucket to drain."""
        rate = Rate.per_second(_BURST)
        limiter = AsyncRateLimiter(rate)
        start = time.monotonic()
        await asyncio.gather(*(limiter.wait() for _ in range(_BURST + 1)))
        # The extra caller waits one drip, rate.period / rate.requests; half of it allows for timer slack.
        assert time.monotonic() - start >= rate.period / rate.requests / 2

    async def test_sub_one_max_rate_preserved_not_clamped(self) -> None:
        """Pace half a request per period as one request per two periods, not a silent clamp up to one."""
        rate = Rate(requests=0.5, period=_SHORT_PERIOD)
        gap = rate.period / rate.requests
        elapsed = await _elapsed_for_second_wait(AsyncRateLimiter(rate))
        assert elapsed >= gap * 3 / 4, f"expected ~{gap}s gap for half a request per period, got {elapsed:.3f}s"


@pytest.mark.transport_policy
def test_a_limiter_used_under_successive_event_loops_gets_a_bucket_per_loop() -> None:
    """Rebuild the bucket for a new loop, so ``aiolimiter`` never warns about (or misbehaves on) reuse across loops."""
    limiter = AsyncRateLimiter(Rate.per_second(1000))
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        asyncio.run(limiter.wait())
        asyncio.run(limiter.wait())


def test_rate_units_are_in_the_constructor() -> None:
    """Name the unit in the constructor: ``per_second`` is a 1 s period, ``per_minute`` a 60 s one."""
    assert Rate.per_second(2) == Rate(requests=2, period=1.0)
    assert Rate.per_minute(120) == Rate(requests=120, period=60.0)


@pytest.mark.parametrize(
    ("requests", "period"),
    [(0, 1.0), (-1, 1.0), (float("nan"), 1.0), (1, 0.0), (1, -60.0), (1, float("nan")), (1, float("inf"))],
)
def test_a_rate_must_be_positive_and_finite(requests: float, period: float) -> None:
    """Refuse a budget that cannot pace, rather than failing on the first request that has to wait."""
    with pytest.raises(ValueError, match="must be > 0"):
        Rate(requests=requests, period=period)


def test_a_sub_one_rate_scales_its_period_rather_than_clamping() -> None:
    """Rescale 0.5 per second to 1 per 2 seconds, the form ``aiolimiter`` accepts."""
    assert AsyncRateLimiter(Rate(requests=0.5, period=1.0)).rate == Rate(requests=1.0, period=2.0)


def test_a_limiter_built_with_policy_off_is_unlimited() -> None:
    """Pace at :data:`UNLIMITED` whatever rate was asked for, when built under ``policy_disabled()``."""
    with policy_disabled():
        limiter = AsyncRateLimiter(Rate.per_second(1))
    assert limiter.rate == UNLIMITED


def test_path_limiters_pick_the_first_matching_rule_and_default_otherwise() -> None:
    """Route a path to the first rule whose fragment it contains, and any other path to the default.

    Both fragments occur in ``/api/candles``, and the ``/api`` rule is declared second, so a last-match scan would
    route it to the ``/api`` limiter instead.
    """
    limiters = PathLimiters(
        default=Rate.per_second(1000),
        rules=[RateRule("/candles", Rate.per_second(1000)), RateRule("/api", Rate.per_second(1000))],
    )
    candles = limiters.for_request(httpx2.Request("GET", "https://x/candles"))
    api = limiters.for_request(httpx2.Request("GET", "https://x/api/ticker"))

    assert candles is not api, "each rule has its own limiter"
    assert limiters.default not in {candles, api}
    assert limiters.for_request(httpx2.Request("GET", "https://x/api/candles?g=1")) is candles, "the first rule wins"
    assert limiters.for_request(httpx2.Request("GET", "https://x/ticker")) is limiters.default
