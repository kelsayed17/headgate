# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""``TestClock`` and ``instant_retries``: time a test drives, through the timing clients are built with."""

from __future__ import annotations

import asyncio
import math

import httpx2
import pytest

from headgate import HttpOptions, Rate, RetryPolicy, TransportPolicy, nothing
from headgate._core import current_timing
from headgate.retry import Backoff
from headgate.testing import FakeTransport, TestClock, instant_retries
from tests.support.clients import StubClient

_START = 100.0
_STEP = 2.5
#: A backoff whose waits are exactly ``factor * 2**retry``: the jitter always draws 1.
_EXACT_BACKOFF = Backoff(factor=1.0, max_wait=60.0, jitter=lambda: 1.0)


def test_the_clock_reads_what_it_was_moved_to() -> None:
    """Read the start, then each advance."""
    clock = TestClock(_START)
    assert clock() == _START
    clock.advance(_STEP)
    assert clock() == _START + _STEP


@pytest.mark.parametrize("step", [-1.0, math.nan, math.inf])
def test_the_clock_refuses_a_step_no_clock_takes(step: float) -> None:
    """Refuse a backwards, NaN or infinite step."""
    with pytest.raises(ValueError, match="only moves forward"):
        TestClock().advance(step)


async def test_a_sleep_advances_the_clock_and_is_recorded() -> None:
    """Advance by the slept seconds without waiting, and record them."""
    clock = TestClock()
    await clock.sleep(_STEP)
    assert (clock(), clock.sleeps) == (_STEP, [_STEP])


async def test_concurrent_sleeps_overlap_as_real_time_does() -> None:
    """Wake each sleeper at its own start plus its delay, earliest first: the clock ends at the latest, not the sum."""
    clock = TestClock(_START)
    woke: list[tuple[str, float]] = []

    async def nap(name: str, *seconds: float) -> None:
        for each in seconds:
            await clock.sleep(each)
            woke.append((name, clock() - _START))

    await asyncio.gather(nap("a", 10.0, 10.0), nap("b", 15.0), nap("c", 30.0), nap("d", 15.0))

    assert woke == [("a", 10.0), ("b", 15.0), ("d", 15.0), ("a", 20.0), ("c", 30.0)]
    assert clock() == _START + 30.0
    assert clock.sleeps == [10.0, 15.0, 30.0, 15.0, 10.0], "each wait is recorded as it begins"


async def test_a_cancelled_sleep_does_not_move_the_clock() -> None:
    """Leave the clock where it was when a sleep is cancelled before it wakes, as a real one would."""
    clock = TestClock()
    sleeper = asyncio.create_task(clock.sleep(60.0))
    await asyncio.sleep(0)
    sleeper.cancel()
    with pytest.raises(asyncio.CancelledError):
        await sleeper
    await clock.sleep(_STEP)
    assert (clock(), clock.sleeps) == (_STEP, [60.0, _STEP])


@pytest.mark.parametrize("step", [-1.0, math.nan, math.inf])
async def test_a_sleep_refuses_a_step_no_clock_takes(step: float) -> None:
    """Refuse a backwards, NaN or infinite sleep, as :meth:`TestClock.advance` refuses the step."""
    with pytest.raises(ValueError, match="only moves forward"):
        await TestClock().sleep(step)


async def test_concurrent_calls_back_off_together() -> None:
    """Retry two concurrent calls that each back off 30 s at t=30, inside their 45 s deadlines, as production would."""
    fake = FakeTransport().reply("GET", "/x", 503, 503, 200, 200)
    clock = TestClock()
    with clock.install():
        client = StubClient(
            base_url="https://h.test",
            rate=Rate.per_second(1000),
            policy=TransportPolicy(
                retry=RetryPolicy(attempts=2, backoff=Backoff(factor=15.0, max_wait=60.0, jitter=lambda: 1.0))
            ),
            http=HttpOptions(transport=fake),
        )

    await asyncio.gather(client.get("x", expect=nothing, deadline=45.0), client.get("x", expect=nothing, deadline=45.0))

    assert clock.sleeps == [30.0, 30.0]
    assert clock() == pytest.approx(30.0)


async def test_a_client_built_under_install_waits_on_the_test_clock() -> None:
    """Sleep the retry backoff on the test clock: each wait recorded, and the clock moved by all of them."""
    fake = FakeTransport().reply("GET", "/x", 503, 503, 200)
    clock = TestClock()
    with clock.install() as installed:
        assert installed is clock
        client = StubClient(
            base_url="https://h.test",
            rate=Rate.per_second(1000),
            policy=TransportPolicy(retry=RetryPolicy(attempts=3, backoff=_EXACT_BACKOFF)),
            http=HttpOptions(transport=fake),
        )
    await client.get("x", expect=nothing)

    assert clock.sleeps == [2.0, 4.0]
    assert clock() == sum(clock.sleeps)


async def test_instant_retries_replaces_only_the_sleep() -> None:
    """Keep the clock in force, and replace the sleep with one that returns at once."""
    clock = TestClock()
    with clock.install(), instant_retries():
        timing = current_timing()
        assert timing.clock is clock
        await timing.sleep(_STEP)
    assert clock.sleeps == []


async def test_instant_retries_retries_without_waiting() -> None:
    """Retry a mocked failure as production does, sending every attempt."""
    fake = FakeTransport().reply("GET", "/x", httpx2.codes.SERVICE_UNAVAILABLE, httpx2.codes.OK)
    with instant_retries():
        client = StubClient(base_url="https://h.test", rate=Rate.per_second(1000), http=HttpOptions(transport=fake))
    await client.get("x", expect=nothing)
    assert len(fake.calls) == len(("503", "200"))
