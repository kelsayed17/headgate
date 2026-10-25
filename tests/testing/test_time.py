# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""``TestClock`` and ``instant_retries``: time a test drives, through the timing clients are built with."""

from __future__ import annotations

import math

import httpx2
import pytest

from headgate import Backoff, HttpOptions, Rate, RetryPolicy, TransportPolicy, nothing
from headgate._core import current_timing
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


async def test_a_client_built_under_install_waits_on_the_test_clock() -> None:
    """Sleep the retry backoff on the test clock: each wait recorded, and the clock moved by all of them."""
    fake = FakeTransport().route("GET", "/x", 503, 503, 200)
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
    fake = FakeTransport().route("GET", "/x", httpx2.codes.SERVICE_UNAVAILABLE, httpx2.codes.OK)
    with instant_retries():
        client = StubClient(base_url="https://h.test", rate=Rate.per_second(1000), http=HttpOptions(transport=fake))
    await client.get("x", expect=nothing)
    assert len(fake.calls) == len(("503", "200"))
