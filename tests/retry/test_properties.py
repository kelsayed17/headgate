# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""The retry invariants, checked by Hypothesis over random fault schedules.

Each example sends one request through ``RetryTransport`` over ``SendTrackingTransport`` and a ``FaultyTransport``
playing a random schedule, on a fake clock, and checks what the call log and the clock recorded:

* a non-idempotent request without an idempotency key is never re-sent after any byte of it was written, unless the
  server proved it was not processed (a refused stream, a 425);
* no attempt starts, and no wait ends, past the deadline less ``min_attempt_time``, and nothing runs past the deadline;
* the retries never exceed the budget;
* every response the call does not return is closed;
* the history notes match the call log, one line per send, and the result is the last send's.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, override

import httpx2
from hypothesis import given, settings
from hypothesis import strategies as st

from headgate._core import CallContext, CallInfo
from headgate.retry import IDEMPOTENT_METHODS, Backoff, RetryBudget, RetryPolicy, RetryTransport, SendTrackingTransport
from headgate.testing import (
    ConnectFail,
    DropMidBody,
    FaultCall,
    FaultyTransport,
    PoolFull,
    RefusedStream,
    Respond,
    Slow,
)
from tests.support.clock import FakeClock

if TYPE_CHECKING:
    from headgate.testing import Step

#: The timeouts a client stamps on a request.
_TIMEOUTS = {"connect": 10.0, "read": 30.0, "write": 10.0, "pool": 15.0}

#: The budget's window, in seconds.
_WINDOW = 10.0

#: The budget's share of first attempts that retries may add.
_RATIO = 0.2

#: Slack for float comparisons on the fake clock.
_EPSILON = 1e-9

#: The status RFC 8470 defines for a request the server did not process.
_TOO_EARLY = 425

_RESPONSES = st.builds(
    Respond,
    status=st.sampled_from([200, 404, 408, 425, 429, 500, 503]),
    retry_after=st.sampled_from([None, 0.5, 2.0, 100.0]),
)

_STEPS = st.one_of(
    st.just(ConnectFail()),
    st.just(ConnectFail(timeout=True)),
    st.just(PoolFull()),
    _RESPONSES,
    st.builds(DropMidBody, after_bytes=st.sampled_from([0, 10, 10_000])),
    st.just(RefusedStream()),
    st.builds(Slow, seconds=st.sampled_from([0.5, 50.0]), then=_RESPONSES),
)


def _frozen() -> float:
    """Return a constant reading, so the budget's window never slides and every retry of a call counts against it."""
    return 0.0


@dataclass(frozen=True, slots=True)
class _Scenario:
    """One example: the request, the policy's knobs, and the network's schedule."""

    method: str
    keyed: bool
    attempts: int
    deadline: float | None
    floor: float
    jitter: float
    schedule: list[Step]


@st.composite
def _scenarios(draw: st.DrawFn) -> _Scenario:
    """Draw a request, a policy and a schedule long enough for every attempt."""
    attempts = draw(st.integers(min_value=1, max_value=5))
    return _Scenario(
        method=draw(st.sampled_from(["GET", "PUT", "POST", "PATCH"])),
        keyed=draw(st.booleans()),
        attempts=attempts,
        deadline=draw(st.none() | st.floats(min_value=0.5, max_value=60.0)),
        floor=draw(st.sampled_from([0.0, 0.1, 10.0])),
        jitter=draw(st.floats(min_value=0.0, max_value=0.999)),
        schedule=draw(st.lists(_STEPS, min_size=attempts, max_size=attempts)),
    )


class _Timed(httpx2.AsyncBaseTransport):
    """Record the clock reading at each send, then pass it on."""

    def __init__(self, transport: httpx2.AsyncBaseTransport, clock: FakeClock) -> None:
        """Time the sends made through *transport* on *clock*."""
        self._transport = transport
        self._clock = clock
        self.starts: list[float] = []

    @override
    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        self.starts.append(self._clock())
        return await self._transport.handle_async_request(request)


@dataclass(slots=True)
class _Run:
    """What one example recorded."""

    calls: list[FaultCall]
    starts: list[float]
    sleeps: list[tuple[float, float]]
    notes: tuple[str, ...]
    end: float
    result: httpx2.Response | Exception


async def _run(scenario: _Scenario) -> _Run:
    """Send the scenario's request and return what happened."""
    clock = FakeClock()
    sleeps: list[tuple[float, float]] = []

    async def retry_sleep(seconds: float, /) -> None:
        sleeps.append((clock(), seconds))
        await clock.sleep(seconds)

    network = FaultyTransport(schedule=scenario.schedule, sleep=clock.sleep)
    timed = _Timed(network, clock)
    policy = RetryPolicy(
        attempts=scenario.attempts,
        backoff=Backoff(jitter=lambda: scenario.jitter),
        budget=RetryBudget(ratio=_RATIO, min_per_second=scenario.floor, window=_WINDOW, clock=_frozen),
    )
    retry = RetryTransport(
        transport=SendTrackingTransport(transport=timed), policy=policy, sleep=retry_sleep, clock=clock
    )
    request = httpx2.Request(
        scenario.method, "https://api.test/orders", json={"qty": 1}, extensions={"timeout": dict(_TIMEOUTS)}
    )
    context = CallContext(
        CallInfo(method=scenario.method, url="https://api.test/orders"), clock=clock, deadline_after=scenario.deadline
    )
    context.idempotency_key = "order-1" if scenario.keyed else None
    context.attach(request)
    result: httpx2.Response | Exception
    try:
        result = await retry.handle_async_request(request)
    except httpx2.HTTPError as exc:
        result = exc
    run = _Run(network.calls, timed.starts, sleeps, context.notes, clock(), result)
    if isinstance(result, httpx2.Response):
        await result.aclose()
    return run


def _proven_unprocessed(call: FaultCall) -> bool:
    """Return whether *call*'s failure proves the server did not process the request."""
    response = call.response
    return isinstance(call.step, RefusedStream) or (response is not None and response.status_code == _TOO_EARLY)


def _check_safety(scenario: _Scenario, run: _Run) -> None:
    """Assert that an unkeyed write is never re-sent after bytes of it were written, unless proven unprocessed."""
    if scenario.method in IDEMPOTENT_METHODS or scenario.keyed:
        return
    for call in run.calls[:-1]:
        assert call.bytes_sent == 0 or _proven_unprocessed(call), f"re-sent after {call.step}"


def _check_deadline(scenario: _Scenario, run: _Run) -> None:
    """Assert that no retry or wait starts too late, and nothing runs past the deadline."""
    if scenario.deadline is None:
        return
    latest = scenario.deadline - RetryPolicy().min_attempt_time + _EPSILON
    assert all(start <= latest for start in run.starts[1:]), run.starts
    assert all(start + seconds <= latest for start, seconds in run.sleeps), run.sleeps
    assert run.end <= scenario.deadline + _EPSILON


def _check_budget(scenario: _Scenario, run: _Run) -> None:
    """Assert that the retries stayed within the budget's allowance for one first attempt."""
    allowance = _RATIO * 1 + scenario.floor * _WINDOW
    assert len(run.calls) - 1 <= allowance


def _check_closed(run: _Run) -> None:
    """Assert that every response the call did not return was closed."""
    for call in run.calls:
        if call.response is not None and call.response is not run.result:
            assert call.response_closed is True, call.step


def _check_history(scenario: _Scenario, run: _Run) -> None:
    """Assert one history line per send, describing it, and that the result is the last send's."""
    history = [note for note in run.notes if note.startswith("attempt ")]
    assert len(history) == len(run.calls)
    for number, (line, call) in enumerate(zip(history, run.calls, strict=True), start=1):
        assert line.startswith(f"attempt {number}/{scenario.attempts}: ")
        described = str(call.response.status_code) if call.response is not None else type(call.error).__name__
        assert described in line
    last = run.calls[-1]
    if isinstance(run.result, Exception):
        assert run.result is last.error
    else:
        assert last.response is not None
        assert run.result.status_code == last.response.status_code


@settings(max_examples=400, deadline=None)
@given(_scenarios())
def test_the_retry_invariants_hold_for_any_fault_schedule(scenario: _Scenario) -> None:
    """Check every invariant of the module docstring on one random scenario."""
    run = asyncio.run(_run(scenario))

    _check_safety(scenario, run)
    _check_deadline(scenario, run)
    _check_budget(scenario, run)
    _check_closed(run)
    _check_history(scenario, run)
