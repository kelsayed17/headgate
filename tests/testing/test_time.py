# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""``TestClock`` and ``instant_retries``: time a test drives, through the timing clients are built with."""

from __future__ import annotations

import asyncio
import dataclasses
import email.utils
import math
import time
from typing import TYPE_CHECKING, Any, cast

import httpx2
import pytest

import headgate.timing
from headgate import HttpOptions, Rate, RateBudget, RetryPolicy, TransportPolicy, nothing
from headgate._core import Timing as CoreTiming
from headgate._core import current_timing
from headgate.breaker import BreakerRegistry, CircuitBreaker
from headgate.errors import UpstreamStatusError
from headgate.retry import Backoff, RetryBudget, RetryShare
from headgate.testing import FakeTransport, TestClock, instant_retries
from headgate.testing._time import _SETTLE_TURNS
from headgate.timing import Timing
from tests.support.clients import StubClient

if TYPE_CHECKING:
    from collections.abc import Callable

_START = 100.0
_STEP = 2.5
#: A backoff whose waits are exactly ``factor * 2**retry`` under a clock whose jitter always draws 1.
_EXACT_BACKOFF = Backoff(factor=1.0, max_wait=60.0)


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


async def test_a_cancelled_sleep_never_moves_the_clock_however_long_the_loop_runs() -> None:
    """Leave the clock where it was through any number of quiet turns once the only sleep is cancelled."""
    clock = TestClock()
    sleeper = asyncio.create_task(clock.sleep(60.0))
    await asyncio.sleep(0)
    sleeper.cancel()
    with pytest.raises(asyncio.CancelledError):
        await sleeper
    for _ in range(_SETTLE_TURNS * 2):
        await asyncio.sleep(0)
    assert clock() == 0.0


async def _sleep_after_turns(clock: TestClock, turns: int, *, woken: bool) -> float:
    """Sleep 5 seconds *turns* turns of the loop after starting (after waking from a first sleep when *woken*).

    Another task sleeps 10 seconds from the start. Return the reading the 5-second sleep woke at.
    """
    other = asyncio.create_task(clock.sleep(10.0))
    if woken:
        await clock.sleep(1.0)
    else:
        await asyncio.sleep(0)
    for _ in range(turns):
        await asyncio.sleep(0)
    await clock.sleep(5.0)
    woke = clock()
    await other
    return woke


@pytest.mark.parametrize("woken", [False, True], ids=["after-a-sleep-begins", "after-a-wake"])
async def test_the_clock_moves_after_exactly_its_settling_turns(*, woken: bool) -> None:
    """Wait ``_SETTLE_TURNS`` quiet turns of the loop before moving: a sleep begun within them overlaps the other.

    The count starts again whenever a sleep begins or wakes. A sleep begun one turn later starts after the clock has
    moved to the other sleeper's wake.
    """
    started = 1.0 if woken else 0.0
    assert await _sleep_after_turns(TestClock(), _SETTLE_TURNS, woken=woken) == started + 5.0
    assert await _sleep_after_turns(TestClock(), _SETTLE_TURNS + 1, woken=woken) == 10.0 + 5.0


async def test_a_cancelled_sleep_due_with_another_is_left_asleep() -> None:
    """Wake the live sleeper and skip a cancelled one due at the same reading, which no one is waiting on."""
    clock = TestClock()
    live = asyncio.create_task(clock.sleep(_STEP))
    cancelled = asyncio.create_task(clock.sleep(_STEP))
    await asyncio.sleep(0)
    cancelled.cancel()
    await live
    with pytest.raises(asyncio.CancelledError):
        await cancelled
    assert clock() == _STEP


@pytest.mark.parametrize("step", [-1.0, math.nan, math.inf])
async def test_a_sleep_refuses_a_step_no_clock_takes(step: float) -> None:
    """Refuse a backwards, NaN or infinite sleep, as :meth:`TestClock.advance` refuses the step."""
    with pytest.raises(ValueError, match="only moves forward"):
        await TestClock().sleep(step)


async def test_concurrent_calls_back_off_together() -> None:
    """Retry two concurrent calls that each back off 30 s at t=30, inside their 45 s deadlines, as production would."""
    fake = FakeTransport().reply("GET", "/x", 503, 503, 200, 200)
    clock = TestClock(random=lambda: 1.0)
    with clock.install():
        client = StubClient(
            base_url="https://h.test",
            rate=Rate.per_second(1000),
            policy=TransportPolicy(retry=RetryPolicy(attempts=2, backoff=Backoff(factor=15.0, max_wait=60.0))),
            http=HttpOptions(transport=fake),
        )

    await asyncio.gather(client.get("x", expect=nothing, deadline=45.0), client.get("x", expect=nothing, deadline=45.0))

    assert clock.sleeps == [30.0, 30.0]
    assert clock() == pytest.approx(30.0)


async def test_a_client_built_under_install_waits_on_the_test_clock() -> None:
    """Sleep the retry backoff on the test clock: each wait recorded, and the clock moved by all of them."""
    fake = FakeTransport().reply("GET", "/x", 503, 503, 200)
    clock = TestClock(random=lambda: 1.0)
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


@pytest.mark.transport_policy
async def test_install_reaches_a_rate_budget_two_clients_share() -> None:
    """Pace two clients that share a budget built in the block on the test clock: their waits are its sleeps."""
    clock = TestClock()
    with clock.install():
        shared = RateBudget(Rate.per_second(1))
        clients = [
            StubClient(
                base_url="https://h.test", rate=shared, http=HttpOptions(transport=FakeTransport().reply("GET", "/x"))
            )
            for _ in range(2)
        ]

    for client in clients:
        await client.get("x", expect=nothing)

    assert clock.now == pytest.approx(1.0), "the second client waited its turn on the test clock"


def test_install_reaches_the_cooldown_of_a_registry() -> None:
    """Run the cooldown of a breaker from a registry built in the block on the test clock."""
    clock = TestClock()
    with clock.install():
        registry = BreakerRegistry()
    _trip(registry)

    clock.advance(29.0)
    assert _breaker(registry).refusing
    clock.advance(1.0)
    assert not _breaker(registry).refusing


def test_what_is_built_outside_the_block_keeps_the_system_clock() -> None:
    """Leave a registry built before the block on the system clock: installing a clock changes nothing already built."""
    outside = BreakerRegistry()
    clock = TestClock()
    with clock.install():
        _trip(outside)
    clock.advance(30.0)

    assert _breaker(outside).refusing, "its cooldown runs in real time, which has not passed"


def _breaker(registry: BreakerRegistry) -> CircuitBreaker:
    """Return *registry*'s breaker for the test origin, tripping after one failure and cooling down for 30 s."""
    return registry.breaker_for("https://h.test", failure_threshold=1, recovery_timeout=30.0)


def _trip(registry: BreakerRegistry) -> None:
    """Open *registry*'s breaker for the test origin with one failure."""
    with _breaker(registry).track() as verdict:
        verdict.mark_failed()


@pytest.mark.parametrize(
    "build",
    [
        lambda: Backoff(**cast("Any", {"jitter": lambda: 1.0})),
        lambda: RateBudget(Rate.per_second(1), **cast("Any", {"wall_clock": time.time})),
        lambda: BreakerRegistry(**cast("Any", {"clock": time.monotonic})),
    ],
    ids=["backoff-jitter", "budget-wall-clock", "registry-clock"],
)
def test_the_old_clock_keywords_are_gone(build: Callable[[], object]) -> None:
    """Refuse ``jitter=``, ``wall_clock=`` and ``clock=``: each is a field of the one ``timing=`` now."""
    with pytest.raises(TypeError):
        build()


def test_the_timing_module_is_the_one_seam() -> None:
    """Export the same ``Timing`` the layers take, with all four seams."""
    assert headgate.timing.Timing is CoreTiming
    assert {field.name for field in dataclasses.fields(Timing)} == {"clock", "sleep", "wall_clock", "random"}


def test_a_retry_budget_built_in_the_block_slides_on_the_test_clock() -> None:
    """Measure a shared retry budget's window on the clock installed when it was built, read once then."""
    clock = TestClock()
    with clock.install():
        budget = RetryBudget(RetryShare(ratio=0.0, min_per_second=0.1, window=10.0))
    assert budget.try_spend()
    assert not budget.try_spend()

    clock.advance(20.0)
    assert budget.try_spend()


async def test_an_errors_retry_after_date_is_read_against_the_clients_wall_clock() -> None:
    """Give an error the wait a ``Retry-After`` HTTP-date asks for, measured from the test clock's epoch."""
    clock = TestClock()
    asked = email.utils.formatdate(clock.wall_clock() + 120, usegmt=True)
    fake = FakeTransport().reply("GET", "/x", httpx2.Response(503, headers={"Retry-After": asked}))
    with clock.install():
        client = StubClient(
            base_url="https://h.test",
            rate=Rate.per_second(1000),
            policy=TransportPolicy(retry=RetryPolicy(max_retry_after=30.0), breaker=None),
            http=HttpOptions(transport=fake),
        )

    with pytest.raises(UpstreamStatusError) as raised:
        await client.get("x", expect=nothing)
    assert raised.value.retry_after == pytest.approx(120.0)


async def test_a_wait_the_server_asks_for_draws_no_random_number() -> None:
    """Draw from the timing's random source for a backoff only, so a seeded source replays whatever servers ask."""
    draws: list[None] = []

    def draw() -> float:
        draws.append(None)
        return 1.0

    clock = TestClock(random=draw)
    fake = FakeTransport().reply("GET", "/x", httpx2.Response(503, headers={"Retry-After": "1"}), 503, 200)
    with clock.install():
        client = StubClient(
            base_url="https://h.test",
            rate=Rate.per_second(1000),
            policy=TransportPolicy(retry=RetryPolicy(attempts=3, backoff=_EXACT_BACKOFF), breaker=None),
            http=HttpOptions(transport=fake),
        )

    await client.get("x", expect=nothing)
    assert len(draws) == 1, "the second retry backs off; the first waits the second the server asked"
    assert clock.sleeps == [1.0, 4.0]
