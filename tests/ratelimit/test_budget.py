# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""``RateBudget`` on its own: pacing, pauses, adaptive throttling, sharing, and the budgets a client resolves.

Every test drives the budget on a fake clock, so the assertions are exact; one smoke test checks real pacing. The
module keeps the real ``RateBudget.wait`` (``transport_policy``), since that wait is the subject.
"""

from __future__ import annotations

import asyncio
import threading
import time

import httpx2
import pytest

from headgate._core import CallContext, CallInfo, Emitter, Event, RateLimited, Timing
from headgate.policy import policy_disabled
from headgate.ratelimit import UNLIMITED, Adaptive, BudgetPausedError, PathBudgets, Rate, RateBudget, RateRule
from tests.support.clock import FakeClock

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


class _Recorder:
    """Collect every event delivered."""

    def __init__(self) -> None:
        """Start empty."""
        self.events: list[Event] = []

    def notify(self, event: Event) -> None:
        """Keep *event*."""
        self.events.append(event)


def _context(clock: FakeClock, *, deadline: float | None = None, observer: _Recorder | None = None) -> CallContext:
    """Return a call's context on *clock*, due *deadline* seconds from now, reporting to *observer*."""
    emitter = Emitter(() if observer is None else (observer,))
    return CallContext(CallInfo(method="GET", url="https://x/"), clock=clock, deadline_after=deadline, emitter=emitter)


def _budget(clock: FakeClock, rate: Rate = _RATE, **options: Adaptive | int | None) -> RateBudget:
    """Return a budget at *rate* on *clock*."""
    adaptive = options.get("adaptive", Adaptive())
    in_flight = options.get("max_in_flight")
    assert adaptive is None or isinstance(adaptive, Adaptive)
    assert in_flight is None or isinstance(in_flight, int)
    return RateBudget(
        rate,
        adaptive=adaptive,
        max_in_flight=in_flight,
        timing=Timing(clock=clock, sleep=clock.sleep),
        wall_clock=clock,
    )


def _response(status: int, headers: dict[str, str] | None = None) -> httpx2.Response:
    """Return a response with *status* and *headers*."""
    return httpx2.Response(status, headers=headers)


def _rate_limited(recorder: _Recorder) -> list[tuple[float, str]]:
    """Return each ``RateLimited`` event's delay and reason."""
    return [(event.delay, event.reason) for event in recorder.events if isinstance(event, RateLimited)]


# --- Pacing ---


async def test_a_burst_goes_at_once_and_then_requests_are_spaced_by_the_interval() -> None:
    """Admit ``rate.requests`` back to back, then one per ``rate.interval``."""
    clock = FakeClock()
    budget = _budget(clock)
    for _ in range(_BURST + 2):
        await budget.wait(_context(clock))
    assert clock.slept == pytest.approx([_RATE.interval, _RATE.interval])


async def test_concurrent_waiters_reserve_successive_turns_in_arrival_order() -> None:
    """Give each waiter beyond the burst the next turn, so the third waits three intervals."""
    clock = FakeClock()
    slept: list[float] = []

    async def record(seconds: float, /) -> None:
        slept.append(seconds)
        await asyncio.sleep(0)

    budget = RateBudget(_RATE, timing=Timing(clock=clock, sleep=record))
    await asyncio.gather(*(budget.wait(_context(clock)) for _ in range(_BURST + 3)))
    assert slept == pytest.approx([_RATE.interval * turn for turn in (1, 2, 3)])


async def test_a_rate_below_one_per_period_spaces_every_request() -> None:
    """Pace half a request per second as one request every two seconds, with no burst beyond the first."""
    clock = FakeClock()
    rate = Rate(requests=0.5, period=1.0)
    budget = _budget(clock, rate)
    await budget.wait(_context(clock))
    await budget.wait(_context(clock))
    assert clock.slept == [rate.interval]


async def test_a_cancelled_waiter_gives_its_turn_back() -> None:
    """Hand a cancelled waiter's turn to the next caller, so a deadline expiring in the queue wastes no send."""
    clock = FakeClock()
    blocked = asyncio.Event()
    slept: list[float] = []

    async def sleep(seconds: float, /) -> None:
        slept.append(seconds)
        await blocked.wait()

    budget = RateBudget(Rate.per_second(1), timing=Timing(clock=clock, sleep=sleep))
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
    clock = FakeClock()
    blocked = asyncio.Event()
    slept: list[float] = []

    async def sleep(seconds: float, /) -> None:
        slept.append(seconds)
        await blocked.wait()

    budget = RateBudget(Rate.per_second(1), timing=Timing(clock=clock, sleep=sleep))
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
    clock = FakeClock()
    budget = _budget(clock, Rate.per_second(1))
    recorder = _Recorder()
    await budget.wait(_context(clock, observer=recorder))
    await budget.wait(_context(clock, observer=recorder))
    assert _rate_limited(recorder) == [(1.0, "paced")]


async def test_a_wait_nobody_observes_builds_no_event() -> None:
    """Skip the event without observers; the wait itself is the same."""
    clock = FakeClock()
    budget = _budget(clock, Rate.per_second(1))
    await budget.wait(_context(clock))
    await budget.wait(_context(clock))
    assert clock.slept == [1.0]


async def test_real_pacing_waits_on_the_system_clock() -> None:
    """Pace on the real clock and sleep by default: a second immediate request waits about one interval."""
    rate = Rate(requests=1, period=0.05)
    budget = RateBudget(rate)
    context = CallContext(CallInfo(method="GET", url="https://x/"))
    await budget.wait(context)
    start = time.monotonic()
    await budget.wait(context)
    assert time.monotonic() - start >= rate.interval / 2


# --- Server pauses ---


async def test_a_retry_after_pauses_the_whole_budget() -> None:
    """Make every later call wait out a 429's ``Retry-After`` before its paced turn."""
    clock = FakeClock()
    budget = _budget(clock, adaptive=None)
    recorder = _Recorder()
    budget.observe(_response(429, {"Retry-After": str(int(_PAUSE))}), sent_at=clock())
    assert budget.pause_remaining() == _PAUSE

    await budget.wait(_context(clock, deadline=_LONG_DEADLINE, observer=recorder))

    assert _rate_limited(recorder) == [(_PAUSE, "paused")]
    assert budget.pause_remaining() == 0


async def test_a_pause_past_the_deadline_raises_at_once() -> None:
    """Refuse a call whose deadline ends inside the pause, naming the seconds left and the status that asked for it."""
    clock = FakeClock()
    budget = _budget(clock)
    budget.observe(_response(503, {"Retry-After": str(int(_PAUSE))}), sent_at=clock())

    with pytest.raises(BudgetPausedError) as raised:
        await budget.wait(_context(clock, deadline=_SHORT_DEADLINE))

    assert (raised.value.retry_after, raised.value.status_code) == (_PAUSE, 503)
    assert clock.slept == [], "a pause that cannot fit is never waited"


async def test_a_pause_that_starts_while_a_call_waits_its_turn_is_waited_too() -> None:
    """Check the pause again after the paced wait, so a call queued before a 429 still honours it."""
    clock = FakeClock()

    async def sleep(seconds: float, /) -> None:
        if not clock.slept:
            budget.observe(_response(429, {"Retry-After": str(int(_PAUSE))}), sent_at=clock())
        await clock.sleep(seconds)

    budget = RateBudget(Rate.per_second(1), adaptive=None, timing=Timing(clock=clock, sleep=sleep), wall_clock=clock)
    await budget.wait(_context(clock))
    await budget.wait(_context(clock))
    assert clock.slept == [1.0, _PAUSE - 1.0], "the pause began when the turn's wait did"


async def test_a_pause_past_the_deadline_found_after_a_paced_wait_raises() -> None:
    """Raise, not wait, when the pause a queued call finds after its turn outlasts its deadline."""
    clock = FakeClock()

    async def sleep(seconds: float, /) -> None:
        budget.observe(_response(429, {"Retry-After": str(int(_LONG_DEADLINE * 2))}), sent_at=clock())
        await clock.sleep(seconds)

    budget = RateBudget(Rate.per_second(1), adaptive=None, timing=Timing(clock=clock, sleep=sleep), wall_clock=clock)
    await budget.wait(_context(clock))
    with pytest.raises(BudgetPausedError):
        await budget.wait(_context(clock, deadline=_LONG_DEADLINE))


async def test_a_shorter_pause_never_shortens_a_longer_one() -> None:
    """Keep the later of two pauses."""
    clock = FakeClock()
    budget = _budget(clock)
    budget.observe(_response(429, {"Retry-After": str(int(_PAUSE))}), sent_at=clock())
    budget.observe(_response(429, {"Retry-After": "1"}), sent_at=clock())
    assert budget.pause_remaining() == _PAUSE


# --- Adaptive throttling ---


async def test_pushback_halves_the_rate_down_to_its_floor() -> None:
    """Multiply the effective rate by ``decrease`` on each new pushback, never below ``min_rate``."""
    clock = FakeClock()
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
    clock = FakeClock()
    budget = _budget(clock)
    sent_at = clock()
    clock.advance(1)
    budget.observe(_response(429), sent_at=sent_at)
    clock.advance(1)
    budget.observe(_response(429), sent_at=sent_at)
    assert budget.effective_rate.requests == _BURST / 2


async def test_success_restores_the_rate_over_time_up_to_the_configured_one() -> None:
    """Add back ``increase`` times the configured rate per second of successes, capped at the configured rate."""
    clock = FakeClock()
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
    clock = FakeClock()
    budget = _budget(clock)
    budget.observe(_response(429), sent_at=clock())
    clock.advance(_RECOVERY)
    budget.observe(_response(500), sent_at=clock())
    assert budget.effective_rate.requests == _BURST / 2


async def test_an_exhausted_quota_header_throttles_and_pauses_a_success() -> None:
    """Treat ``RateLimit: r=0`` on a 200 as pushback: throttle, and pause for its ``t``."""
    clock = FakeClock()
    budget = _budget(clock)
    budget.observe(_response(200, {"RateLimit": '"default";r=0;t=7'}), sent_at=clock())
    assert (budget.effective_rate.requests, budget.pause_remaining()) == (_BURST / 2, 7.0)


async def test_a_throttled_wait_says_so() -> None:
    """Report a wait paced at a throttled rate as ``throttled``."""
    clock = FakeClock()
    budget = _budget(clock, Rate.per_second(1))
    recorder = _Recorder()
    budget.observe(_response(429), sent_at=clock())
    await budget.wait(_context(clock, observer=recorder))
    await budget.wait(_context(clock, observer=recorder))
    assert _rate_limited(recorder) == [(2.0, "throttled")]


async def test_a_budget_without_adaptive_throttling_keeps_its_rate() -> None:
    """Never change the rate with ``adaptive=None``."""
    clock = FakeClock()
    budget = _budget(clock, adaptive=None)
    budget.observe(_response(429), sent_at=clock())
    assert budget.effective_rate == _RATE
    assert budget.adaptive is None


# --- Construction and resolution ---


def test_a_budget_reports_its_configuration() -> None:
    """Expose the configured rate, the throttling and the concurrency cap."""
    budget = RateBudget(_RATE, max_in_flight=2)
    assert (budget.rate, budget.adaptive, budget.max_in_flight, budget.in_flight) == (_RATE, Adaptive(), 2, 0)
    assert RateBudget(_RATE).max_in_flight is None


@pytest.mark.parametrize("cap", [0, -1])
def test_a_budget_refuses_a_concurrency_cap_below_one(cap: int) -> None:
    """Refuse a cap that would admit nothing."""
    with pytest.raises(ValueError, match="max_in_flight"):
        RateBudget(_RATE, max_in_flight=cap)


async def test_a_budget_without_a_cap_lends_no_slot() -> None:
    """Return a release that does nothing when there is no concurrency cap."""
    budget = RateBudget(_RATE)
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
    budget = RateBudget(rate, timing=Timing(clock=lambda: 0.0, sleep=record))

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
    budget = RateBudget(_RATE, max_in_flight=2)
    first = await budget.occupy()
    await budget.occupy()
    first()
    first()
    assert budget.in_flight == 1
