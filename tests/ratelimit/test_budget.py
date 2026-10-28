# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""``RateBudget`` on its own: pacing, pauses, adaptive throttling, sharing, and the budgets a client resolves.

Every test drives the budget on a fake clock, so the assertions are exact; one smoke test checks real pacing. The
module keeps the real ``Pacer.wait`` (``transport_policy``), since that wait is the subject.
"""

from __future__ import annotations

import asyncio
import heapq
import itertools
import threading
import time
from typing import TYPE_CHECKING, override

import httpx2
import pytest

from headgate._core import BudgetBacklogError, CallContext, CallInfo, Emitter, RateLimited, Timing
from headgate.policy import policy_disabled
from headgate.ratelimit import UNLIMITED, Adaptive, Rate, RateBudget, RateRule
from headgate.ratelimit._budget import MAX_PAUSE, BudgetPausedError, Pacer, pacer_of
from headgate.ratelimit._budgets import PathBudgets
from headgate.ratelimit._transport import RateLimitTransport
from headgate.testing import TestClock
from tests.support.events import Recorder

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Coroutine

    from headgate._core import Clock

pytestmark = pytest.mark.transport_policy

#: A burst size, and the rate whose burst it is.
_BURST = 5
_RATE = Rate.per_second(_BURST)

#: A server pause, a deadline shorter than it, and one longer.
_PAUSE = 3.0
_SHORT_DEADLINE = 1.0
_LONG_DEADLINE = 10.0

#: Seconds of successful traffic that restore part of a decreased rate.
_RECOVERY = 2.0

#: How many waits each thread makes against the shared budget.
_PER_THREAD = 4
_THREADS = 3


def _context(clock: Clock, *, deadline: float | None = None, observer: Recorder | None = None) -> CallContext:
    """Return a call's context on *clock*, due *deadline* seconds from now, reporting to *observer*."""
    emitter = Emitter(() if observer is None else (observer,))
    return CallContext(CallInfo(method="GET", url="https://x/"), clock=clock, deadline_after=deadline, emitter=emitter)


def _budget(clock: TestClock, rate: Rate = _RATE, **options: Adaptive | int | None) -> Pacer:
    """Return a budget at *rate* on *clock*."""
    adaptive = options.get("adaptive", Adaptive())
    in_flight = options.get("max_in_flight")
    assert adaptive is None or isinstance(adaptive, Adaptive)
    assert in_flight is None or isinstance(in_flight, int)
    return Pacer(
        rate,
        adaptive=adaptive,
        max_in_flight=in_flight,
        timing=Timing(clock=clock, sleep=clock.sleep),
        wall_clock=clock,
    )


def _response(status: int, headers: dict[str, str] | None = None) -> httpx2.Response:
    """Return a response with *status* and *headers*."""
    return httpx2.Response(status, headers=headers)


def _rate_limited(recorder: Recorder) -> list[tuple[float, str]]:
    """Return each ``RateLimited`` event's delay and reason."""
    return [(event.delay, event.reason) for event in recorder.events if isinstance(event, RateLimited)]


#: How many times the timeline lets the event loop run before it takes every task to be asleep.
_SETTLE = 50


class _Timeline:
    """A clock and a sleep for concurrent waiters, as real time runs them: each sleeper wakes at its own time.

    Unlike :class:`~headgate.testing.TestClock`, sleeps overlap: :meth:`run` lets every task run until all of them
    sleep, then moves the clock to the earliest wake and wakes that sleeper alone (ties in the order they slept).
    """

    def __init__(self) -> None:
        """Start at zero with nobody asleep."""
        self.now = 0.0
        self._sleepers: list[tuple[float, int, asyncio.Future[None]]] = []
        self._order = itertools.count()

    def __call__(self) -> float:
        """Return the current reading."""
        return self.now

    async def sleep(self, seconds: float, /) -> None:
        """Sleep until the clock reaches now plus *seconds*."""
        woken = asyncio.get_running_loop().create_future()
        heapq.heappush(self._sleepers, (self.now + seconds, next(self._order), woken))
        await woken

    @property
    def timing(self) -> Timing:
        """This clock and its sleep."""
        return Timing(clock=self, sleep=self.sleep)

    async def run(self, *calls: Coroutine[object, object, None]) -> None:
        """Run *calls* together to the end, moving the clock from one wake to the next."""
        tasks = [asyncio.create_task(call) for call in calls]
        while True:
            for _ in range(_SETTLE):
                await asyncio.sleep(0)
            if all(task.done() for task in tasks):
                break
            assert self._sleepers, "every task waits on something no sleep will end"
            when, _, woken = heapq.heappop(self._sleepers)
            self.now = max(self.now, when)
            woken.set_result(None)
        for task in tasks:
            task.result()


# --- Pacing ---


async def test_a_burst_goes_at_once_and_then_requests_are_spaced_by_the_interval() -> None:
    """Admit ``rate.requests`` back to back, then one per ``rate.interval``."""
    clock = TestClock()
    budget = _budget(clock)
    for _ in range(_BURST + 2):
        await budget.wait(_context(clock))
    assert clock.sleeps == pytest.approx([_RATE.interval, _RATE.interval])


async def test_concurrent_waiters_reserve_successive_turns_in_arrival_order() -> None:
    """Give each waiter beyond the burst the next turn, so the third waits three intervals."""
    clock = TestClock()
    slept: list[float] = []

    async def record(seconds: float, /) -> None:
        slept.append(seconds)
        await asyncio.sleep(0)

    budget = Pacer(_RATE, timing=Timing(clock=clock, sleep=record))
    await asyncio.gather(*(budget.wait(_context(clock)) for _ in range(_BURST + 3)))
    assert slept == pytest.approx([_RATE.interval * turn for turn in (1, 2, 3)])


async def test_a_rate_below_one_per_period_spaces_every_request() -> None:
    """Pace half a request per second as one request every two seconds, with no burst beyond the first."""
    clock = TestClock()
    rate = Rate(requests=0.5, period=1.0)
    budget = _budget(clock, rate)
    await budget.wait(_context(clock))
    await budget.wait(_context(clock))
    assert clock.sleeps == [rate.interval]


async def test_a_cancelled_waiter_gives_its_turn_back() -> None:
    """Hand a cancelled waiter's turn to the next caller, so a deadline expiring in the queue wastes no send."""
    clock = TestClock()
    blocked = asyncio.Event()
    slept: list[float] = []

    async def sleep(seconds: float, /) -> None:
        slept.append(seconds)
        await blocked.wait()

    budget = Pacer(Rate.per_second(1), timing=Timing(clock=clock, sleep=sleep))
    await budget.wait(_context(clock))
    waiter = asyncio.create_task(budget.wait(_context(clock)))
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    blocked.set()
    await budget.wait(_context(clock))
    assert slept == [1.0, 1.0], "the next caller waits one interval, not two"


async def test_a_cancelled_waiter_behind_a_later_one_keeps_the_queue_intact() -> None:
    """Refund nothing when a later caller already reserved after the cancelled one: its turn stays in the schedule."""
    clock = TestClock()
    blocked = asyncio.Event()
    slept: list[float] = []

    async def sleep(seconds: float, /) -> None:
        slept.append(seconds)
        await blocked.wait()

    budget = Pacer(Rate.per_second(1), timing=Timing(clock=clock, sleep=sleep))
    await budget.wait(_context(clock))
    first = asyncio.create_task(budget.wait(_context(clock)))
    second = asyncio.create_task(budget.wait(_context(clock)))
    await asyncio.sleep(0)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    blocked.set()
    await second
    assert slept == [1.0, 2.0]


async def test_every_wait_is_reported_with_its_reason() -> None:
    """Emit ``RateLimited`` for a paced wait, nothing for an admission without one."""
    clock = TestClock()
    budget = _budget(clock, Rate.per_second(1))
    recorder = Recorder()
    await budget.wait(_context(clock, observer=recorder))
    await budget.wait(_context(clock, observer=recorder))
    assert _rate_limited(recorder) == [(1.0, "paced")]


async def test_a_wait_nobody_observes_builds_no_event() -> None:
    """Skip the event without observers; the wait itself is the same."""
    clock = TestClock()
    budget = _budget(clock, Rate.per_second(1))
    await budget.wait(_context(clock))
    await budget.wait(_context(clock))
    assert clock.sleeps == [1.0]


async def test_real_pacing_waits_on_the_system_clock() -> None:
    """Pace on the real clock and sleep by default: a second immediate request waits about one interval."""
    rate = Rate(requests=1, period=0.05)
    budget = Pacer(rate)
    context = CallContext(CallInfo(method="GET", url="https://x/"))
    await budget.wait(context)
    start = time.monotonic()
    await budget.wait(context)
    assert time.monotonic() - start >= rate.interval / 2


def test_a_budget_reads_out_what_its_pacer_saw() -> None:
    """Report, through the public budget, the pause and the lowered rate a 429 left on the pacer it spends through."""
    clock = TestClock()
    budget = RateBudget(_RATE, timing=clock.timing, wall_clock=clock)
    assert (budget.pause_remaining, budget.effective_rate) == (0, _RATE)

    pacer_of(budget).observe(_response(429, {"Retry-After": str(int(_PAUSE))}), sent_at=clock())

    assert budget.pause_remaining == _PAUSE
    assert budget.effective_rate.requests == _BURST / 2


# --- Server pauses ---


async def test_a_retry_after_pauses_the_whole_budget() -> None:
    """Make every later call wait out a 429's ``Retry-After`` before its paced turn."""
    clock = TestClock()
    budget = _budget(clock, adaptive=None)
    recorder = Recorder()
    budget.observe(_response(429, {"Retry-After": str(int(_PAUSE))}), sent_at=clock())
    assert budget.pause_remaining() == _PAUSE

    await budget.wait(_context(clock, deadline=_LONG_DEADLINE, observer=recorder))

    assert _rate_limited(recorder) == [(_PAUSE, "paused")]
    assert budget.pause_remaining() == 0


async def test_a_pause_past_the_deadline_raises_at_once() -> None:
    """Refuse a call whose deadline ends inside the pause, naming the seconds left and the status that asked for it."""
    clock = TestClock()
    budget = _budget(clock)
    budget.observe(_response(503, {"Retry-After": str(int(_PAUSE))}), sent_at=clock())

    with pytest.raises(BudgetPausedError) as raised:
        await budget.wait(_context(clock, deadline=_SHORT_DEADLINE))

    assert (raised.value.retry_after, raised.value.status_code) == (_PAUSE, 503)
    assert clock.sleeps == [], "a pause that cannot fit is never waited"


async def test_a_turn_past_the_deadline_raises_at_once_and_gives_the_turn_back() -> None:
    """Refuse a call whose paced turn comes at or after its deadline, without waiting; the next call takes the turn."""
    clock = TestClock()
    budget = _budget(clock, Rate.per_second(1), adaptive=None)
    await budget.wait(_context(clock))

    with pytest.raises(BudgetBacklogError) as raised:
        await budget.wait(_context(clock, deadline=_SHORT_DEADLINE))

    assert raised.value.retry_after == 1.0
    assert str(raised.value) == "the rate budget's turn in 1s comes after the call's deadline"
    assert clock.sleeps == [], "a turn that cannot fit is never waited"
    await budget.wait(_context(clock, deadline=_LONG_DEADLINE))
    assert clock.sleeps == [1.0], "the refused turn was given back"


async def test_a_pause_that_starts_while_a_call_waits_its_turn_is_waited_too() -> None:
    """Check the pause again after the paced wait, so a call queued before a 429 still honors it."""
    clock = TestClock()

    async def sleep(seconds: float, /) -> None:
        if not clock.sleeps:
            budget.observe(_response(429, {"Retry-After": str(int(_PAUSE))}), sent_at=clock())
        await clock.sleep(seconds)

    budget = Pacer(Rate.per_second(1), adaptive=None, timing=Timing(clock=clock, sleep=sleep), wall_clock=clock)
    await budget.wait(_context(clock))
    await budget.wait(_context(clock))
    assert clock.sleeps == [1.0, _PAUSE - 1.0], "the pause began when the turn's wait did"


async def test_a_pause_past_the_deadline_found_after_a_paced_wait_raises() -> None:
    """Raise, not wait, when the pause a queued call finds after its turn outlasts its deadline."""
    clock = TestClock()

    async def sleep(seconds: float, /) -> None:
        budget.observe(_response(429, {"Retry-After": str(int(_LONG_DEADLINE * 2))}), sent_at=clock())
        await clock.sleep(seconds)

    budget = Pacer(Rate.per_second(1), adaptive=None, timing=Timing(clock=clock, sleep=sleep), wall_clock=clock)
    await budget.wait(_context(clock))
    with pytest.raises(BudgetPausedError):
        await budget.wait(_context(clock, deadline=_LONG_DEADLINE))


async def test_a_shorter_pause_never_shortens_a_longer_one() -> None:
    """Keep the later of two pauses."""
    clock = TestClock()
    budget = _budget(clock)
    budget.observe(_response(429, {"Retry-After": str(int(_PAUSE))}), sent_at=clock())
    budget.observe(_response(429, {"Retry-After": "1"}), sent_at=clock())
    assert budget.pause_remaining() == _PAUSE


async def test_a_pause_holds_the_turns_already_reserved_in_order_and_at_the_rate() -> None:
    """Send the calls queued before a pause one interval apart after it, in their order, not all at its end."""
    timeline = _Timeline()
    rate = Rate(requests=1, period=0.1)
    budget = Pacer(rate, adaptive=None, timing=timeline.timing, wall_clock=timeline)
    sent: list[tuple[int, float]] = []

    async def call(number: int) -> None:
        await budget.wait(_context(timeline))
        sent.append((number, timeline.now))

    async def pushback() -> None:
        await timeline.sleep(0.05)
        budget.observe(_response(429, {"retry-after-ms": "500"}), sent_at=timeline.now)

    await timeline.run(pushback(), *(call(number) for number in range(8)))

    assert [number for number, _ in sent] == list(range(8))
    assert [when for _, when in sent] == pytest.approx([0.0, *(0.55 + rate.interval * turn for turn in range(7))])


async def test_a_call_arriving_during_a_pause_queues_behind_the_turns_it_postponed() -> None:
    """Keep FIFO across a pause: a call that arrives while it runs sends after every call queued before it."""
    timeline = _Timeline()
    budget = Pacer(Rate(requests=1, period=1.0), adaptive=None, timing=timeline.timing, wall_clock=timeline)
    sent: list[str] = []

    async def call(name: str, *, after: float = 0.0) -> None:
        await timeline.sleep(after)
        await budget.wait(_context(timeline))
        sent.append(name)

    async def pushback() -> None:
        await timeline.sleep(0.5)
        budget.observe(_response(503, {"Retry-After": "2"}), sent_at=timeline.now)

    await timeline.run(pushback(), call("first"), call("second"), call("third"), call("late", after=1.0))

    assert sent == ["first", "second", "third", "late"]
    assert timeline.now == pytest.approx(4.5), "the queue resumed at 2.5, one second apart"


async def test_a_turn_postponed_by_a_pause_reports_its_wait_after_the_pause_as_the_pauses() -> None:
    """Report a queued call's wait for its postponed turn as ``paused`` even once the pause itself has ended."""
    timeline = _Timeline()
    budget = Pacer(Rate(requests=1, period=1.0), adaptive=None, timing=timeline.timing, wall_clock=timeline)
    recorder = Recorder()

    async def pushback() -> None:
        await timeline.sleep(0.5)
        budget.observe(_response(503, {"Retry-After": "2"}), sent_at=timeline.now)

    await timeline.run(
        pushback(),
        budget.wait(_context(timeline)),
        budget.wait(_context(timeline)),
        budget.wait(_context(timeline, observer=recorder)),
    )

    # Its turn was at 2; the pause to 2.5 moved the queue by 1.5, so it is due at 3.5, a second after the pause ends.
    assert _rate_limited(recorder) == [(2.0, "paced"), (0.5, "paused"), (1.0, "paused")]


async def test_a_second_pause_moves_the_queue_on_from_where_the_first_left_it() -> None:
    """Shift the turns queued behind a pause again when a longer one follows, from where the first pause put them."""
    timeline = _Timeline()
    budget = Pacer(Rate(requests=1, period=1.0), adaptive=None, timing=timeline.timing, wall_clock=timeline)
    sent: list[float] = []

    async def call() -> None:
        await budget.wait(_context(timeline))
        sent.append(timeline.now)

    async def pushback() -> None:
        await timeline.sleep(0.5)
        budget.observe(_response(503, {"Retry-After": "2"}), sent_at=timeline.now)
        budget.observe(_response(503, {"Retry-After": "4"}), sent_at=timeline.now)

    await timeline.run(pushback(), call(), call(), call())

    assert sent == pytest.approx([0.0, 4.5, 5.5]), "the queue resumes as the longer pause ends, one second apart"


async def test_a_cancelled_waiters_turn_does_not_hold_the_queue_at_a_pause() -> None:
    """Shift the queue at a pause from the turns still waiting, not from the turn of a waiter cancelled before it."""
    clock = TestClock()
    blocked = asyncio.Event()
    slept: list[float] = []

    async def sleep(seconds: float, /) -> None:
        slept.append(seconds)
        await blocked.wait()
        await clock.sleep(seconds)

    budget = Pacer(Rate.per_second(1), adaptive=None, timing=Timing(clock=clock, sleep=sleep), wall_clock=clock)
    await budget.wait(_context(clock))
    first = asyncio.create_task(budget.wait(_context(clock)))
    second = asyncio.create_task(budget.wait(_context(clock)))
    await asyncio.sleep(0)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    budget.observe(_response(429, {"Retry-After": "4"}), sent_at=clock())
    blocked.set()
    await second

    assert slept == [1.0, 2.0, 2.0], "the second waiter's turn at 2 moves to the pause's end at 4, and no later"


@pytest.mark.parametrize("retry_after", ["99999999999", "Fri, 31 Dec 9999 23:59:59 GMT"], ids=["seconds", "date"])
async def test_a_pause_is_cut_to_an_hour(retry_after: str) -> None:
    """Pause no longer than ``MAX_PAUSE`` however long a response asks, so a hostile header cannot stop the budget."""
    clock = TestClock()
    budget = _budget(clock)

    asked = budget.observe(_response(503, {"Retry-After": retry_after}), sent_at=clock())
    await budget.wait(_context(clock))

    assert (asked, clock.sleeps) == (MAX_PAUSE, [MAX_PAUSE])
    assert MAX_PAUSE == 60 * 60


class _Wire(httpx2.AsyncBaseTransport):
    """Answer each request after the time *durations* gives it, the first with a 429 asking for *pause*; log sends."""

    def __init__(self, timeline: _Timeline, durations: list[float], *, pause: float | None = None) -> None:
        """Take *durations[n]* seconds over request *n*."""
        self._timeline = timeline
        self._durations = durations
        self._pause = pause
        self.sent: list[float] = []

    @override
    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        number = len(self.sent)
        self.sent.append(self._timeline.now)
        await self._timeline.sleep(self._durations[number])
        if number == 0 and self._pause is not None:
            return httpx2.Response(429, headers={"Retry-After": f"{self._pause:g}"}, stream=_Body())
        return httpx2.Response(200, stream=_Body())


class _Body(httpx2.AsyncByteStream):
    """A streamed empty body, so the concurrency slot is held until it is closed."""

    @override
    async def __aiter__(self) -> AsyncIterator[bytes]:
        return
        yield b""


async def _send_all(budget: RateBudget, wire: _Wire, timeline: _Timeline, calls: int) -> None:
    """Send *calls* requests at once through a rate-limit layer spending *budget* over *wire*, closing each answer."""
    layer = RateLimitTransport(budgets=PathBudgets(default=budget), transport=wire)

    async def call() -> None:
        response = await layer.handle_async_request(httpx2.Request("GET", "https://x/"))
        await response.aclose()

    await timeline.run(*(call() for _ in range(calls)))


async def test_a_call_waiting_for_a_slot_waits_out_a_pause_set_meanwhile() -> None:
    """Hold the calls a freed slot lets through behind the pause the first answer asked for."""
    timeline = _Timeline()
    budget = RateBudget(Rate.per_second(100), max_in_flight=2, adaptive=None, timing=timeline.timing)
    wire = _Wire(timeline, [0.1] * 6, pause=1.0)

    await _send_all(budget, wire, timeline, 6)

    assert wire.sent == pytest.approx([0.0, 0.0, 1.1, 1.1, 1.2, 1.2])
    assert budget.in_flight == 0


async def test_calls_freed_together_by_slots_still_go_out_at_the_rate() -> None:
    """Pace the calls that slots freed at one instant let through, rather than sending them all at once."""
    timeline = _Timeline()
    budget = RateBudget(Rate(requests=1, period=1.0), max_in_flight=2, adaptive=None, timing=timeline.timing)
    wire = _Wire(timeline, [10.0, 9.0, 1.0, 1.0])

    await _send_all(budget, wire, timeline, 4)

    assert wire.sent == pytest.approx([0.0, 1.0, 10.0, 11.0])


# --- Adaptive throttling ---


async def test_pushback_halves_the_rate_down_to_its_floor() -> None:
    """Multiply the effective rate by ``decrease`` on each new pushback, never below ``min_rate``."""
    clock = TestClock()
    budget = _budget(clock, adaptive=Adaptive(decrease=0.5, min_rate=0.2))
    expected = [_BURST * 0.5, _BURST * 0.25, _BURST * 0.2]
    seen: list[float] = []
    for _ in expected:
        clock.advance(1)
        budget.observe(_response(429), sent_at=clock())
        seen.append(budget.effective_rate.requests)
    assert seen == pytest.approx(expected)


async def test_one_burst_of_refusals_decreases_the_rate_once() -> None:
    """Ignore pushback on requests sent before the last decrease: concurrent refusals are one signal."""
    clock = TestClock()
    budget = _budget(clock)
    sent_at = clock()
    clock.advance(1)
    budget.observe(_response(429), sent_at=sent_at)
    clock.advance(1)
    budget.observe(_response(429), sent_at=sent_at)
    assert budget.effective_rate.requests == _BURST / 2


async def test_success_restores_the_rate_over_time_up_to_the_configured_one() -> None:
    """Add back ``increase`` times the configured rate per second of successes, capped at the configured rate."""
    clock = TestClock()
    budget = _budget(clock, adaptive=Adaptive(decrease=0.5, increase=0.1))
    budget.observe(_response(503), sent_at=clock())
    clock.advance(_RECOVERY)
    budget.observe(_response(200), sent_at=clock())
    assert budget.effective_rate.requests == pytest.approx(_BURST * (0.5 + 0.1 * _RECOVERY))
    clock.advance(_LONG_DEADLINE)
    budget.observe(_response(404), sent_at=clock())
    assert budget.effective_rate == _RATE


async def test_a_server_error_neither_throttles_nor_restores() -> None:
    """Leave the rate alone on a 500: it says nothing about load the budget controls."""
    clock = TestClock()
    budget = _budget(clock)
    budget.observe(_response(429), sent_at=clock())
    clock.advance(_RECOVERY)
    budget.observe(_response(500), sent_at=clock())
    assert budget.effective_rate.requests == _BURST / 2


async def test_an_exhausted_quota_header_throttles_and_pauses_a_success() -> None:
    """Treat ``RateLimit: r=0`` on a 200 as pushback: throttle, and pause for its ``t``."""
    clock = TestClock()
    budget = _budget(clock)
    budget.observe(_response(200, {"RateLimit": '"default";r=0;t=7'}), sent_at=clock())
    assert (budget.effective_rate.requests, budget.pause_remaining()) == (_BURST / 2, 7.0)


async def test_a_throttled_wait_says_so() -> None:
    """Report a wait paced at a throttled rate as ``throttled``."""
    clock = TestClock()
    budget = _budget(clock, Rate.per_second(1))
    recorder = Recorder()
    budget.observe(_response(429), sent_at=clock())
    await budget.wait(_context(clock, observer=recorder))
    await budget.wait(_context(clock, observer=recorder))
    assert _rate_limited(recorder) == [(2.0, "throttled")]


async def test_a_budget_without_adaptive_throttling_keeps_its_rate() -> None:
    """Never change the rate with ``adaptive=None``."""
    clock = TestClock()
    budget = _budget(clock, adaptive=None)
    budget.observe(_response(429), sent_at=clock())
    assert budget.effective_rate == _RATE
    assert budget.adaptive is None


# --- Construction and resolution ---


def test_a_budget_reports_its_configuration() -> None:
    """Expose the configured rate, the throttling and the concurrency cap."""
    budget = Pacer(_RATE, max_in_flight=2)
    assert (budget.rate, budget.adaptive, budget.max_in_flight, budget.in_flight) == (_RATE, Adaptive(), 2, 0)
    assert Pacer(_RATE).max_in_flight is None


@pytest.mark.parametrize("cap", [0, -1])
def test_a_budget_refuses_a_concurrency_cap_below_one(cap: int) -> None:
    """Refuse a cap that would admit nothing."""
    with pytest.raises(ValueError, match="max_in_flight"):
        Pacer(_RATE, max_in_flight=cap)


async def test_a_budget_without_a_cap_lends_no_slot() -> None:
    """Return a release that does nothing when there is no concurrency cap."""
    budget = Pacer(_RATE)
    release = await budget.occupy()
    release()
    assert budget.in_flight == 0


def test_a_shared_budget_is_used_as_it_is_and_rules_can_share_one() -> None:
    """Resolve a given budget to itself, so two clients or two rules given one budget spend from it together."""
    shared = RateBudget(_RATE)
    budgets = PathBudgets(default=shared, rules=[RateRule("/a", shared), RateRule("/b", Rate.per_second(1))])
    a = budgets.for_request(httpx2.Request("GET", "https://x/a"))
    b = budgets.for_request(httpx2.Request("GET", "https://x/b"))
    assert budgets.default is shared
    assert a is shared
    assert b is not shared
    assert b.rate == Rate.per_second(1)


def test_path_budgets_pick_the_first_matching_rule_and_default_otherwise() -> None:
    """Route a path to the first rule whose fragment it contains, and any other path to the default."""
    budgets = PathBudgets(
        default=Rate.per_second(1000),
        rules=[RateRule("/candles", Rate.per_second(1000)), RateRule("/api", Rate.per_second(1000))],
    )
    candles = budgets.for_request(httpx2.Request("GET", "https://x/candles"))
    api = budgets.for_request(httpx2.Request("GET", "https://x/api/ticker"))

    assert candles is not api, "each rule has its own budget"
    assert budgets.default not in {candles, api}
    assert budgets.for_request(httpx2.Request("GET", "https://x/api/candles?g=1")) is candles, "the first rule wins"
    assert budgets.for_request(httpx2.Request("GET", "https://x/ticker")) is budgets.default


def test_budgets_built_with_policy_off_are_private_and_unlimited() -> None:
    """Replace every budget, a shared one included, with a private unlimited one that never throttles."""
    shared = RateBudget(Rate.per_second(1), max_in_flight=1)
    with policy_disabled():
        budgets = PathBudgets(default=shared, rules=[RateRule("/r", Rate.per_second(1))])
    rule = budgets.for_request(httpx2.Request("GET", "https://x/r"))
    for budget in (budgets.default, rule):
        assert budget is not shared
        assert (budget.rate, budget.adaptive, budget.max_in_flight) == (UNLIMITED, None, None)


# --- Sharing across loops and threads ---


def test_one_budget_shared_by_clients_on_several_threads_books_every_turn_once() -> None:
    """Pace waits from several threads, each running its own event loop, as one budget: no turn is handed out twice.

    The clock never moves, so every turn past the burst is a reservation further into the future; one schedule shared
    across the threads gives exactly the delays one interval, two intervals, and so on.
    """
    lock = threading.Lock()
    slept: list[float] = []

    async def record(seconds: float, /) -> None:
        with lock:
            slept.append(seconds)

    rate = Rate.per_second(2)
    budget = Pacer(rate, timing=Timing(clock=lambda: 0.0, sleep=record))

    async def spend() -> None:
        context = CallContext(CallInfo(method="GET", url="https://x/"), clock=lambda: 0.0)
        for _ in range(_PER_THREAD):
            await budget.wait(context)

    threads = [threading.Thread(target=asyncio.run, args=(spend(),)) for _ in range(_THREADS)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    turns = _PER_THREAD * _THREADS - int(rate.burst)
    assert sorted(slept) == pytest.approx([rate.interval * turn for turn in range(1, turns + 1)])


async def test_a_slot_given_back_twice_is_freed_once() -> None:
    """Make a slot's release idempotent, so two closes of one body cannot free another holder's slot."""
    budget = Pacer(_RATE, max_in_flight=2)
    first = await budget.occupy()
    await budget.occupy()
    first()
    first()
    assert budget.in_flight == 1


# --- Exact edges ---


async def test_a_longer_pause_extends_a_running_one() -> None:
    """Move the end of a running pause later when a later response asks for longer."""
    clock = TestClock()
    budget = _budget(clock)
    budget.observe(_response(429, {"Retry-After": str(int(_PAUSE))}), sent_at=clock())
    clock.advance(1.0)
    budget.observe(_response(429, {"Retry-After": str(int(_LONG_DEADLINE))}), sent_at=clock())

    assert budget.pause_remaining() == _LONG_DEADLINE


async def test_a_pause_ending_at_the_same_instant_keeps_the_status_that_asked_first() -> None:
    """Keep the first status when a second pause would end at exactly the same instant."""
    clock = TestClock()
    budget = _budget(clock)
    budget.observe(_response(429, {"Retry-After": str(int(_PAUSE))}), sent_at=clock())
    budget.observe(_response(503, {"Retry-After": str(int(_PAUSE))}), sent_at=clock())

    with pytest.raises(BudgetPausedError) as raised:
        await budget.wait(_context(clock, deadline=_SHORT_DEADLINE))

    assert raised.value.status_code == httpx2.codes.TOO_MANY_REQUESTS


async def test_a_pause_exactly_as_long_as_the_time_left_is_waited() -> None:
    """Wait a pause that ends exactly at the call's deadline; only a longer one is refused."""
    clock = TestClock()
    budget = _budget(clock, adaptive=None)
    budget.observe(_response(429, {"Retry-After": str(int(_PAUSE))}), sent_at=clock())

    await budget.wait(_context(clock, deadline=_PAUSE))

    assert clock.sleeps == [_PAUSE]


async def test_a_turn_due_now_is_taken_whatever_time_the_call_has_left() -> None:
    """Admit a call whose turn has come, even with no time left: the budget refuses only a wait that cannot fit."""
    clock = TestClock()
    # With no burst, the turn is due exactly now rather than before it.
    budget = _budget(clock, Rate.per_second(1))

    await budget.wait(_context(clock, deadline=0.0))

    assert clock.sleeps == []


async def test_a_turn_due_as_a_pause_ends_at_the_deadline_is_waited() -> None:
    """Wait for a turn that falls due exactly as a pause ends, exactly at the call's deadline."""
    clock = TestClock()
    budget = _budget(clock, Rate.per_second(1), adaptive=None)
    await budget.wait(_context(clock))
    budget.observe(_response(429, {"Retry-After": "1"}), sent_at=clock())

    await budget.wait(_context(clock, deadline=1.0))

    assert clock.sleeps == [1.0]


async def test_a_refused_call_gives_back_the_turn_it_reserved() -> None:
    """Hand the turn of a call refused after its paced wait to the next call, which then waits only for the pause."""
    clock = TestClock()
    interval = 4.0
    observed = False

    async def sleep(seconds: float, /) -> None:
        nonlocal observed
        if not observed:
            observed = True
            budget.observe(_response(429, {"Retry-After": "5"}), sent_at=clock())
        await clock.sleep(seconds)

    rate = Rate(requests=1, period=interval)
    budget = Pacer(rate, adaptive=None, timing=Timing(clock=clock, sleep=sleep), wall_clock=clock)
    await budget.wait(_context(clock))
    with pytest.raises(BudgetPausedError):
        await budget.wait(_context(clock, deadline=interval + 0.5))
    await budget.wait(_context(clock))

    assert clock.sleeps == [interval, 1.0], "the refused call's turn went back, so only the pause is waited"


async def test_an_absolute_reset_is_measured_from_the_wall_clock() -> None:
    """Pause until an ``X-RateLimit-Reset`` given as a Unix time, measured on the budget's wall clock."""
    clock = TestClock()
    wall = 1_700_000_000.0
    reset_in = 42.0
    budget = Pacer(_RATE, timing=Timing(clock=clock, sleep=clock.sleep), wall_clock=lambda: wall)
    headers = {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": f"{wall + reset_in:.0f}"}

    budget.observe(_response(200, headers), sent_at=clock())

    assert budget.pause_remaining() == reset_in


async def test_pushback_to_a_request_sent_at_the_decrease_decreases_again() -> None:
    """Decrease again for pushback to a request sent at the very instant of the last decrease, not before it."""
    clock = TestClock()
    budget = _budget(clock, adaptive=Adaptive(decrease=0.5, min_rate=0.1))

    budget.observe(_response(429), sent_at=clock())
    budget.observe(_response(429), sent_at=clock())

    assert budget.effective_rate == Rate(requests=_BURST * 0.25, period=1.0)


async def test_recovery_never_lifts_the_rate_past_the_configured_one() -> None:
    """Cap recovery at the configured rate, so a later pushback halves that rate and not more."""
    clock = TestClock()
    budget = _budget(clock, adaptive=Adaptive(decrease=0.5, increase=0.1))
    budget.observe(_response(429), sent_at=clock())
    clock.advance(1000.0)
    budget.observe(_response(200), sent_at=clock())
    assert budget.effective_rate == _RATE

    budget.observe(_response(429), sent_at=clock())

    assert budget.effective_rate == Rate(requests=_BURST * 0.5, period=1.0)
