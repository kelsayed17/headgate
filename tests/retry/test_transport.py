# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""``RetryTransport`` driven directly over a ``FaultyTransport``: what is re-sent, when, and what is recorded.

Each test builds a request with its own :class:`~headgate._core.CallContext` on a fake clock, so deadlines, waits and
the budget's window move only when the transport sleeps.
"""

from __future__ import annotations

import asyncio
import email.utils
from typing import TYPE_CHECKING, override

import httpx2
import pytest

from headgate._core import (
    BREAKER_GATE,
    BudgetBacklogError,
    CallContext,
    CallInfo,
    Emitter,
    RetryScheduled,
    _retry_after,
)
from headgate.retry import NO_RETRY, Backoff, Hedge, RetryBudget, RetryPolicy
from headgate.retry._policy import REPLAYABLE_BODY, RETRY_OUTCOME, RETRY_POLICY, RetryOutcome
from headgate.retry._transport import RetryTransport
from headgate.testing import (
    ConnectFail,
    DropMidBody,
    FaultyTransport,
    PoolFull,
    RefusedStream,
    Respond,
    Slow,
    TestClock,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from headgate._core import Event
    from headgate.testing import Step

#: The timeouts a client stamps on a request, as httpx2 carries them.
_TIMEOUTS = {"connect": 10.0, "read": 30.0, "write": 10.0, "pool": 15.0}

#: A backoff that always waits its full nominal delay, so waits are predictable.
_FULL = Backoff(jitter=lambda: 1.0)

#: Three attempts with predictable waits and no budget, the policy most tests run under.
_POLICY = RetryPolicy(backoff=_FULL, budget=None)

#: A ``Retry-After`` value a 503 sends in the tests.
_RETRY_AFTER = 2.0

#: How long a hedged request waits before its hedge starts.
_HEDGE_AFTER = 0.5

#: The number of sends in a hedged round with one hedge: the first and the hedge.
_BOTH = 2

#: Sends in a call that fails once and then succeeds.
_TWO_SENDS = 2

#: Sends in a call that uses all three default attempts.
_THREE_SENDS = 3


class _Events:
    """An observer that keeps every event."""

    def __init__(self) -> None:
        """Start with no events."""
        self.events: list[Event] = []

    def notify(self, event: Event) -> None:
        """Keep *event*."""
        self.events.append(event)


def _request(
    method: str = "GET",
    *,
    clock: TestClock,
    deadline: float | None = None,
    key: str | None = None,
    observer: _Events | None = None,
) -> tuple[httpx2.Request, CallContext]:
    """Build a request with a JSON body and timeouts, and a context on *clock* due *deadline* seconds from now."""
    request = httpx2.Request(method, "https://api.test/orders", json={"qty": 1}, extensions={"timeout": _TIMEOUTS})
    emitter = Emitter((observer,)) if observer is not None else Emitter()
    context = CallContext(
        CallInfo(method=method, url="https://api.test/orders"), clock=clock, deadline_after=deadline, emitter=emitter
    )
    context.idempotency_key = key
    context.attach(request)
    return request, context


def _transport(
    schedule: list[Step], clock: TestClock, policy: RetryPolicy = _POLICY
) -> tuple[RetryTransport, FaultyTransport]:
    """Return a retry transport under *policy* over a faulty network playing *schedule*, both on *clock*."""
    network = FaultyTransport(schedule=schedule, sleep=clock.sleep)
    return RetryTransport(transport=network, policy=policy, sleep=clock.sleep, clock=clock), network


def _attempt_notes(context: CallContext) -> list[str]:
    """Return the history lines among *context*'s notes."""
    return [note for note in context.notes if note.startswith("attempt ")]


# --- What is re-sent ---


async def test_an_idempotent_request_is_resent_until_it_succeeds() -> None:
    """Re-send a GET past two 503s to the 200 behind them, backing off in between."""
    clock = TestClock()
    retry, network = _transport([Respond(503), Respond(503), Respond(200)], clock)
    request, context = _request(clock=clock)

    response = await retry.handle_async_request(request)

    assert response.status_code == httpx2.codes.OK
    assert len(network.calls) == context.attempt == _THREE_SENDS
    assert clock.sleeps == [_FULL.delay(1), _FULL.delay(2)]
    assert [call.response_closed for call in network.calls] == [True, True, False]
    await response.aclose()


async def test_running_out_of_attempts_returns_the_last_failed_response() -> None:
    """Return the last 503 for the client to raise, once the attempts are spent."""
    clock = TestClock()
    retry, network = _transport([Respond(503)] * 3, clock)
    request, context = _request(clock=clock)

    response = await retry.handle_async_request(request)

    assert response.status_code == httpx2.codes.SERVICE_UNAVAILABLE
    assert len(network.calls) == _THREE_SENDS
    assert context.get(RETRY_OUTCOME) is not None
    await response.aclose()


async def test_running_out_of_attempts_raises_the_last_exception() -> None:
    """Re-raise the last network failure once the attempts are spent."""
    clock = TestClock()
    retry, network = _transport([ConnectFail()] * 3, clock)
    request, _ = _request(clock=clock)

    with pytest.raises(httpx2.ConnectError):
        await retry.handle_async_request(request)
    assert len(network.calls) == _THREE_SENDS


@pytest.mark.parametrize(
    "fault",
    [
        pytest.param(Respond(503), id="503"),
        pytest.param(Slow(seconds=60), id="read-timeout-after-sending"),
        pytest.param(DropMidBody(after_bytes=10), id="dropped-mid-write"),
    ],
)
async def test_a_post_that_may_have_been_processed_is_not_resent(fault: Step) -> None:
    """Send a POST without an idempotency key once when the failure came after bytes were written."""
    clock = TestClock()
    retry, network = _transport([fault, Respond(200)], clock)
    request, context = _request("POST", clock=clock)

    try:
        response = await retry.handle_async_request(request)
    except httpx2.TransportError:
        pass
    else:
        await response.aclose()
    assert len(network.calls) == 1
    outcome = context.get(RETRY_OUTCOME)
    assert outcome is not None
    assert outcome.retryable is False


@pytest.mark.parametrize(
    "fault",
    [
        pytest.param(ConnectFail(), id="connect-error"),
        pytest.param(ConnectFail(timeout=True), id="connect-timeout"),
        pytest.param(PoolFull(), id="pool-timeout"),
        pytest.param(DropMidBody(after_bytes=0), id="dropped-before-writing"),
        pytest.param(RefusedStream(), id="refused-stream"),
        pytest.param(Respond(425), id="too-early"),
    ],
)
async def test_a_post_that_was_not_processed_is_resent(fault: Step) -> None:
    """Re-send a POST when nothing of it was written, or the server proved it did not process it."""
    clock = TestClock()
    retry, network = _transport([fault, Respond(200)], clock)
    request, _ = _request("POST", clock=clock)

    response = await retry.handle_async_request(request)

    assert response.status_code == httpx2.codes.OK
    assert len(network.calls) == _TWO_SENDS
    await response.aclose()


async def test_a_post_with_an_idempotency_key_is_resent() -> None:
    """Re-send a keyed POST after a failure that came after its bytes were written."""
    clock = TestClock()
    retry, network = _transport([Respond(503), Slow(seconds=60), Respond(201)], clock)
    request, _ = _request("POST", clock=clock, key="order-1")

    response = await retry.handle_async_request(request)

    assert response.status_code == httpx2.codes.CREATED
    assert len(network.calls) == _THREE_SENDS
    await response.aclose()


async def test_a_key_on_a_request_no_client_built_is_read_from_its_header() -> None:
    """Treat a bare request's ``Idempotency-Key`` header as its key."""
    clock = TestClock()
    retry, network = _transport([Respond(503), Respond(200)], clock)
    request = httpx2.Request("POST", "https://api.test/orders", headers={"Idempotency-Key": "k"})

    response = await retry.handle_async_request(request)

    assert len(network.calls) == _TWO_SENDS
    await response.aclose()


async def test_a_local_mistake_is_never_resent() -> None:
    """Raise a ``LocalProtocolError`` at once: the same request cannot succeed."""

    def refuse(request: httpx2.Request) -> httpx2.Response:
        msg = "illegal header value"
        raise httpx2.LocalProtocolError(msg, request=request)

    clock = TestClock()
    retry = RetryTransport(transport=httpx2.MockTransport(refuse), policy=_POLICY, sleep=clock.sleep, clock=clock)
    request, context = _request(clock=clock)

    with pytest.raises(httpx2.LocalProtocolError):
        await retry.handle_async_request(request)
    assert context.attempt == 1


async def test_an_exception_outside_the_network_errors_propagates_at_once() -> None:
    """Let a bug below (anything not an httpx2 error or ``OSError``) propagate without a retry or a history line."""

    def broken(_request: httpx2.Request) -> httpx2.Response:
        msg = "bug"
        raise RuntimeError(msg)

    clock = TestClock()
    retry = RetryTransport(transport=httpx2.MockTransport(broken), policy=_POLICY, sleep=clock.sleep, clock=clock)
    request, context = _request(clock=clock)

    with pytest.raises(RuntimeError, match="bug"):
        await retry.handle_async_request(request)
    assert context.attempt == 1
    assert _attempt_notes(context) == []


async def test_a_body_that_cannot_be_replayed_is_sent_once() -> None:
    """Send once when a layer marked the call's body as not replayable, however safe a retry would otherwise be."""
    clock = TestClock()
    retry, network = _transport([Respond(503), Respond(200)], clock)
    request, context = _request(clock=clock)
    replayable = False
    context.put(REPLAYABLE_BODY, replayable)

    response = await retry.handle_async_request(request)

    assert response.status_code == httpx2.codes.SERVICE_UNAVAILABLE
    assert len(network.calls) == 1
    await response.aclose()


async def test_a_calls_own_policy_wins_over_the_transports() -> None:
    """Use the policy in the call context's retry slot over the transport's default."""
    clock = TestClock()
    retry, network = _transport([Respond(503), Respond(200)], clock)
    request, context = _request(clock=clock)
    context.put(RETRY_POLICY, NO_RETRY)

    response = await retry.handle_async_request(request)

    assert len(network.calls) == 1
    await response.aclose()


async def test_a_request_without_a_context_gets_a_standalone_one() -> None:
    """Stamp a standalone context, naming the origin and path only, on a request no client call built."""
    clock = TestClock()
    retry, _ = _transport([Respond(200)], clock)
    request = httpx2.Request("GET", "https://user:pw@api.test/x?api_key=secret#frag")

    response = await retry.handle_async_request(request)

    context = CallContext.find(request)
    assert context is not None
    assert context.info.url == "https://api.test/x"
    assert context.attempt == 1
    await response.aclose()


# --- Retry-After ---


async def test_a_retry_after_is_waited_exactly() -> None:
    """Wait a 503's ``Retry-After`` exactly, past the backoff's cap."""
    clock = TestClock()
    long_wait = 20.0
    retry, _ = _transport([Respond(503, retry_after=long_wait), Respond(200)], clock)
    request, _ = _request(clock=clock)

    response = await retry.handle_async_request(request)

    assert clock.sleeps == [long_wait]
    await response.aclose()


async def test_a_retry_after_past_the_ceiling_stops_at_once() -> None:
    """Return the 429 without waiting when its ``Retry-After`` exceeds ``max_retry_after``, noting why."""
    clock = TestClock()
    retry, network = _transport([Respond(429, retry_after=100), Respond(200)], clock)
    request, context = _request(clock=clock)

    response = await retry.handle_async_request(request)

    assert response.status_code == httpx2.codes.TOO_MANY_REQUESTS
    assert len(network.calls) == 1
    assert clock.sleeps == []
    assert "Retry-After 100.0s exceeds max_retry_after 30.0s" in context.notes
    assert context.get(RETRY_OUTCOME) == RetryOutcome(retryable=True, retry_after=100.0)
    assert network.calls[0].response_closed is False, "the returned response is left for the caller to read"
    await response.aclose()


async def test_a_long_failure_body_is_parked_in_part() -> None:
    """Keep a bounded head of a long failure body through the wait, closing the original."""
    clock = TestClock()
    retry, network = _transport(
        [Respond(503, content=b"x" * 100_000), Respond(503)], clock, RetryPolicy(attempts=2, budget=None)
    )
    request, _ = _request(clock=clock)

    response = await retry.handle_async_request(request)

    assert network.calls[0].response_closed is True
    await response.aclose()


async def test_the_default_sleep_is_asyncio_sleep() -> None:
    """Wait through ``asyncio.sleep`` when no sleep is injected."""
    network = FaultyTransport(schedule=[Respond(503, retry_after=0), Respond(200)])
    retry = RetryTransport(transport=network, policy=RetryPolicy(budget=None))
    request = httpx2.Request("GET", "https://api.test/x")

    response = await retry.handle_async_request(request)

    assert response.status_code == httpx2.codes.OK
    await response.aclose()


# --- The deadline ---


async def test_each_attempt_is_cut_to_the_deadline() -> None:
    """Cap every timeout at the time left, so an attempt cannot outlive the call's deadline."""
    clock = TestClock()
    seen: list[dict[str, float]] = []

    def record(request: httpx2.Request) -> httpx2.Response:
        seen.append(dict(request.extensions["timeout"]))
        clock.advance(4.0)
        return httpx2.Response(503 if len(seen) == 1 else 200)

    policy = RetryPolicy(backoff=Backoff(factor=0.0), budget=None)
    retry = RetryTransport(transport=httpx2.MockTransport(record), policy=policy, sleep=clock.sleep, clock=clock)
    request, _ = _request(clock=clock, deadline=12.0)

    response = await retry.handle_async_request(request)

    assert seen == [
        {"connect": 10.0, "read": 12.0, "write": 10.0, "pool": 12.0},
        {"connect": 8.0, "read": 8.0, "write": 8.0, "pool": 8.0},
    ]
    assert request.extensions["timeout"] == _TIMEOUTS, "the request's own timeouts are restored afterwards"
    await response.aclose()


async def test_a_timeout_left_unset_is_bounded_by_the_deadline() -> None:
    """Give a phase with no timeout of its own the time left before the deadline."""
    clock = TestClock()
    seen: list[dict[str, float | None]] = []

    def record(request: httpx2.Request) -> httpx2.Response:
        seen.append(dict(request.extensions["timeout"]))
        return httpx2.Response(200)

    retry = RetryTransport(transport=httpx2.MockTransport(record), policy=_POLICY, sleep=clock.sleep, clock=clock)
    request, _ = _request(clock=clock, deadline=5.0)
    request.extensions["timeout"] = {"connect": None, "read": 30.0, "write": None, "pool": None}

    await retry.handle_async_request(request)

    assert seen == [{"connect": 5.0, "read": 5.0, "write": 5.0, "pool": 5.0}]


async def test_no_wait_is_started_that_would_overrun_the_deadline() -> None:
    """Stop with "deadline exhausted" rather than sleep past the deadline, and never attempt after it."""
    clock = TestClock()
    retry, network = _transport([Respond(503, retry_after=10), Respond(200)], clock)
    request, context = _request(clock=clock, deadline=5.0)

    response = await retry.handle_async_request(request)

    assert response.status_code == httpx2.codes.SERVICE_UNAVAILABLE
    assert len(network.calls) == 1
    assert clock.sleeps == []
    assert "deadline exhausted" in context.notes
    await response.aclose()


async def test_a_slow_attempt_that_eats_the_deadline_is_not_followed_by_another() -> None:
    """Stop when an attempt timed out at the deadline, since no time is left for another."""
    clock = TestClock()
    retry, network = _transport([Slow(seconds=60), Respond(200)], clock)
    request, context = _request(clock=clock, deadline=3.0)

    with pytest.raises(httpx2.ReadTimeout):
        await retry.handle_async_request(request)

    assert len(network.calls) == 1
    assert clock.now == pytest.approx(3.0), "the attempt's read timeout was cut to the deadline"
    assert "deadline exhausted" in context.notes


async def test_a_deadline_reached_during_the_wait_stops_before_the_next_attempt() -> None:
    """Check the deadline again after the wait, returning the parked failure instead of attempting."""
    clock = TestClock()

    async def oversleep(seconds: float, /) -> None:
        await clock.sleep(seconds + 10.0)

    network = FaultyTransport(schedule=[Respond(503, content=b"busy"), Respond(200)], sleep=clock.sleep)
    retry = RetryTransport(transport=network, policy=_POLICY, sleep=oversleep, clock=clock)
    request, context = _request(clock=clock, deadline=5.0)

    response = await retry.handle_async_request(request)

    assert response.status_code == httpx2.codes.SERVICE_UNAVAILABLE
    assert await response.aread() == b"busy", "the parked copy keeps the failure's body"
    assert len(network.calls) == 1
    assert network.calls[0].response_closed is True, "the original was read and closed before the wait"
    assert context.notes[-1] == "deadline exhausted"


# --- The breaker and the budget ---


async def test_an_open_breaker_stops_the_sequence() -> None:
    """Stop before the next attempt when the breaker gate refuses, noting "circuit opened"."""
    clock = TestClock()
    retry, network = _transport([ConnectFail(), Respond(200)], clock)
    request, context = _request(clock=clock)
    context.put(BREAKER_GATE, lambda: True)

    with pytest.raises(httpx2.ConnectError):
        await retry.handle_async_request(request)

    assert len(network.calls) == 1
    assert "circuit opened" in context.notes


async def test_a_breaker_opening_during_the_wait_stops_before_the_next_attempt() -> None:
    """Re-check the gate after the wait, returning the parked failure when it opened meanwhile."""
    clock = TestClock()
    gate_open = False

    async def open_breaker(seconds: float, /) -> None:
        nonlocal gate_open
        gate_open = True
        await clock.sleep(seconds)

    network = FaultyTransport(schedule=[Respond(503), Respond(200)], sleep=clock.sleep)
    retry = RetryTransport(transport=network, policy=_POLICY, sleep=open_breaker, clock=clock)
    request, context = _request(clock=clock)
    context.put(BREAKER_GATE, lambda: gate_open)

    response = await retry.handle_async_request(request)

    assert response.status_code == httpx2.codes.SERVICE_UNAVAILABLE
    assert len(network.calls) == 1
    assert context.notes[-1] == "circuit opened"
    await response.aclose()


async def test_a_closed_breaker_lets_the_retries_run() -> None:
    """Keep retrying while the gate answers that the breaker is not refusing."""
    clock = TestClock()
    retry, network = _transport([Respond(503), Respond(200)], clock)
    request, context = _request(clock=clock)
    context.put(BREAKER_GATE, lambda: False)

    response = await retry.handle_async_request(request)

    assert len(network.calls) == _TWO_SENDS
    await response.aclose()


async def test_a_spent_budget_stops_the_sequence_and_the_error_stays_retryable() -> None:
    """Stop when the budget has no room, noting it, with the failure still marked retryable."""
    clock = TestClock()
    budget = RetryBudget(ratio=0.0, min_per_second=0.1, window=10.0)
    policy = RetryPolicy(backoff=_FULL, budget=budget)
    retry, network = _transport([Respond(503), Respond(503), Respond(200)], clock, policy)
    request, context = _request(clock=clock)

    response = await retry.handle_async_request(request)

    assert response.status_code == httpx2.codes.SERVICE_UNAVAILABLE
    assert len(network.calls) == _TWO_SENDS, "the budget allows one retry"
    assert context.notes[-1] == "retry budget exhausted"
    outcome = context.get(RETRY_OUTCOME)
    assert outcome is not None
    assert outcome.retryable is True
    await response.aclose()


async def test_one_budget_is_shared_by_every_call_through_the_transport() -> None:
    """Charge every call's retries to the transport's one ledger for an equal budget value."""
    clock = TestClock()
    policy = RetryPolicy(backoff=_FULL, budget=RetryBudget(ratio=0.0, min_per_second=0.1, window=10.0))
    retry, network = _transport([Respond(503), Respond(200), Respond(503)], clock, policy)

    first, _ = _request(clock=clock)
    await (await retry.handle_async_request(first)).aclose()
    second, second_context = _request(clock=clock)
    second_context.put(RETRY_POLICY, RetryPolicy(backoff=_FULL, budget=RetryBudget(ratio=0.0, min_per_second=0.1)))
    await (await retry.handle_async_request(second)).aclose()

    assert len(network.calls) == _THREE_SENDS, "the second call's retry finds the shared budget spent"
    assert second_context.notes[-1] == "retry budget exhausted"


#: A budget with room for exactly one retry over its window, whatever the traffic.
_ONE_RETRY = RetryBudget(ratio=0.0, min_per_second=0.1, window=10.0)


class _Answers(httpx2.AsyncBaseTransport):
    """Answer each send with the next of *answers*: a status, or an exception raised."""

    def __init__(self, *answers: int | Exception) -> None:
        """Answer in order."""
        self._answers = list(answers)

    @override
    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        answer = self._answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return httpx2.Response(answer, request=request)


async def _second_call_retries(retry: RetryTransport, clock: TestClock) -> None:
    """Assert that a call through *retry* after a 503 is retried to its 200: the budget still has its one retry."""
    request, context = _request(clock=clock)
    response = await retry.handle_async_request(request)
    assert (response.status_code, context.attempt) == (httpx2.codes.OK, _TWO_SENDS), "the budget had no retry left"
    await response.aclose()


async def test_a_retry_stopped_after_its_wait_gives_its_budget_back() -> None:
    """Refund the retry a call paid for when the breaker opens during the wait, so the next call can retry."""
    clock = TestClock()
    opened = False

    async def open_breaker(seconds: float, /) -> None:
        nonlocal opened
        opened = True
        await clock.sleep(seconds)

    network = _Answers(503, 503, 200)
    retry = RetryTransport(transport=network, policy=RetryPolicy(backoff=_FULL, budget=_ONE_RETRY), sleep=open_breaker)
    request, context = _request(clock=clock)
    context.put(BREAKER_GATE, lambda: opened)
    await (await retry.handle_async_request(request)).aclose()
    assert context.notes[-1] == "circuit opened"

    opened = False
    await _second_call_retries(retry, clock)


async def test_a_retry_the_rate_budget_refuses_gives_its_budget_back() -> None:
    """Refund the retry a call paid for when the rate budget cannot send it before the deadline."""
    clock = TestClock()
    backlog = BudgetBacklogError("the rate budget's turn comes after the call's deadline", retry_after=1.0)
    network = _Answers(503, backlog, 503, 200)
    retry = RetryTransport(transport=network, policy=RetryPolicy(backoff=_FULL, budget=_ONE_RETRY), sleep=clock.sleep)
    request, context = _request(clock=clock)
    await (await retry.handle_async_request(request)).aclose()
    assert context.notes[-1] == "deadline exhausted"

    await _second_call_retries(retry, clock)


async def test_a_retry_whose_call_is_cancelled_in_the_wait_gives_its_budget_back() -> None:
    """Refund the retry a call paid for when the call is cancelled during the wait before it."""
    clock = TestClock()
    cancel = True

    async def sleep(seconds: float, /) -> None:
        if cancel:
            raise asyncio.CancelledError
        await clock.sleep(seconds)

    network = _Answers(503, 503, 200)
    retry = RetryTransport(transport=network, policy=RetryPolicy(backoff=_FULL, budget=_ONE_RETRY), sleep=sleep)
    request, _ = _request(clock=clock)
    with pytest.raises(asyncio.CancelledError):
        await retry.handle_async_request(request)

    cancel = False
    await _second_call_retries(retry, clock)


async def test_the_backoff_counts_retries_not_the_hedges_beside_them() -> None:
    """Wait the first retry's backoff after a round in which the first send and its hedge both failed."""
    clock = TestClock()
    observer = _Events()
    retry, _ = _transport(
        [Slow(seconds=1, then=Respond(503)), Respond(503), Respond(200)],
        clock,
        RetryPolicy(backoff=_FULL, budget=None, hedge=Hedge(after=_HEDGE_AFTER)),
    )
    request, _ = _request(clock=clock, observer=observer)

    await (await retry.handle_async_request(request)).aclose()

    scheduled = [event for event in observer.events if isinstance(event, RetryScheduled)]
    assert [(event.reason, event.delay) for event in scheduled] == [("hedge", _HEDGE_AFTER), ("503", _FULL.delay(1))]


async def test_the_wait_is_the_retry_after_the_attempt_was_judged_by(monkeypatch: pytest.MonkeyPatch) -> None:
    """Wait the HTTP-date ``Retry-After`` as classification read it, not as read again on a later wall clock."""
    clock = TestClock()
    readings = iter(range(1_700_000_000, 1_700_000_100))
    monkeypatch.setattr(_retry_after.time, "time", lambda: float(next(readings)))
    date = email.utils.formatdate(1_700_000_010, usegmt=True)
    retry, _ = _transport([Respond(503, headers={"Retry-After": date}), Respond(200)], clock)
    request, context = _request(clock=clock)

    await (await retry.handle_async_request(request)).aclose()

    outcome_line = _attempt_notes(context)[0]
    assert outcome_line == "attempt 1/3: 503 Service Unavailable (Retry-After 10.0s) after 0.0s"
    assert clock.sleeps == [10.0], "the wait is the delay the history names"


# --- History and events ---


async def test_every_attempt_adds_a_history_line() -> None:
    """Record each attempt's outcome and duration, with the ``Retry-After`` it asked for."""
    clock = TestClock()
    retry, _ = _transport([Respond(503, retry_after=_RETRY_AFTER), Slow(seconds=0.41, then=Respond(503))], clock)
    request, context = _request(clock=clock, deadline=60.0)
    policy = RetryPolicy(attempts=2, backoff=_FULL, budget=None)
    context.put(RETRY_POLICY, policy)

    response = await retry.handle_async_request(request)

    assert _attempt_notes(context) == [
        "attempt 1/2: 503 Service Unavailable (Retry-After 2.0s) after 0.0s",
        "attempt 2/2: 503 Service Unavailable after 0.41s",
    ]
    await response.aclose()


async def test_a_history_line_names_an_exception_by_type_only() -> None:
    """Name a failure by its type, and a refused stream by its error code, never by its message."""
    clock = TestClock()
    retry, _ = _transport([ConnectFail(), RefusedStream(), Respond(200)], clock)
    request, context = _request(clock=clock)

    response = await retry.handle_async_request(request)

    assert _attempt_notes(context) == [
        "attempt 1/3: ConnectError after 0.0s",
        "attempt 2/3: RemoteProtocolError (REFUSED_STREAM) after 0.0s",
        "attempt 3/3: 200 OK after 0.0s",
    ]
    await response.aclose()


async def test_a_status_without_a_reason_phrase_is_named_by_its_code() -> None:
    """Write a bare code for a status with no registered phrase."""
    clock = TestClock()
    retry, _ = _transport([Respond(599)], clock, RetryPolicy(statuses=frozenset({599}), attempts=1))
    request, context = _request(clock=clock)

    await (await retry.handle_async_request(request)).aclose()

    assert _attempt_notes(context) == ["attempt 1/1: 599 after 0.0s"]


async def test_every_retry_is_announced_with_its_delay_and_reason() -> None:
    """Emit ``RetryScheduled`` before each wait, numbering the attempt to come."""
    clock = TestClock()
    observer = _Events()
    retry, _ = _transport([Respond(503, retry_after=_RETRY_AFTER), ConnectFail(), Respond(200)], clock)
    request, _ = _request(clock=clock, observer=observer)

    await (await retry.handle_async_request(request)).aclose()

    scheduled = [event for event in observer.events if isinstance(event, RetryScheduled)]
    assert [(event.attempt, event.delay, event.reason) for event in scheduled] == [
        (2, _RETRY_AFTER, "503"),
        (3, _FULL.delay(2), "ConnectError"),
    ]


# --- Hedging ---


class _Changes:
    """An event set whenever a test double records something, so a test can wait for a condition without polling."""

    def __init__(self) -> None:
        """Start with nothing recorded."""
        self._event = asyncio.Event()

    def note(self) -> None:
        """Wake whoever waits: something was recorded."""
        self._event.set()

    async def until(self, condition: Callable[[], object]) -> None:
        """Wait until *condition* returns something true, re-checking each time something is recorded."""
        while not condition():
            self._event.clear()
            await self._event.wait()


class _Gate:
    """A sleep that waits until the test opens it, so a hedge fires exactly when the test decides."""

    def __init__(self, changes: _Changes) -> None:
        """Start closed, reporting each wait to *changes*."""
        self._changes = changes
        self.opened = asyncio.Event()
        self.waits: list[float] = []

    async def sleep(self, seconds: float, /) -> None:
        """Record *seconds* and wait for the gate."""
        self.waits.append(seconds)
        self._changes.note()
        await self.opened.wait()


class _Held(httpx2.AsyncBaseTransport):
    """A network whose sends each wait for their own release, answering with the status given then."""

    def __init__(self, changes: _Changes) -> None:
        """Start with no sends, reporting each send and response to *changes*."""
        self._changes = changes
        self.started: list[asyncio.Future[int]] = []
        self.responses: list[httpx2.Response] = []
        self.cancelled = 0

    @override
    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        answer = asyncio.get_running_loop().create_future()
        self.started.append(answer)
        self._changes.note()
        try:
            status = await answer
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        response = httpx2.Response(status, request=request)
        self.responses.append(response)
        self._changes.note()
        return response


def _hedging(policy: RetryPolicy) -> tuple[RetryTransport, _Held, _Gate, _Changes]:
    """Return a retry transport under *policy* over a held network, with the gate its hedge timer waits on."""
    changes = _Changes()
    network, gate = _Held(changes), _Gate(changes)
    return RetryTransport(transport=network, policy=policy, sleep=gate.sleep), network, gate, changes


async def _hedge_started(network: _Held, gate: _Gate, changes: _Changes) -> None:
    """Wait for the first send and the hedge timer, open the gate, and wait for the hedge to be sent."""
    await changes.until(lambda: len(network.started) == 1 and gate.waits)
    gate.opened.set()
    await changes.until(lambda: len(network.started) == _BOTH)


async def test_a_hedge_starts_after_the_delay_and_the_first_answer_wins() -> None:
    """Send a second copy once ``after`` passes, take the first answer, and cancel the other send."""
    observer = _Events()
    retry, network, gate, changes = _hedging(RetryPolicy(hedge=Hedge(after=_HEDGE_AFTER), budget=None))
    request, context = _request(clock=TestClock(), observer=observer)

    call = asyncio.create_task(retry.handle_async_request(request))
    await _hedge_started(network, gate, changes)
    network.started[1].set_result(200)
    response = await call

    assert response.status_code == httpx2.codes.OK
    assert network.cancelled == 1, "the first, slower send is cancelled"
    assert gate.waits[0] == _HEDGE_AFTER
    scheduled = [event for event in observer.events if isinstance(event, RetryScheduled)]
    assert [(event.reason, event.delay) for event in scheduled] == [("hedge", _HEDGE_AFTER)]
    assert _attempt_notes(context) == [
        "attempt 2/3 (hedge): 200 OK after 0.0s",
        "attempt 1/3: cancelled after 0.0s",
    ]
    await response.aclose()


async def test_a_losing_hedges_response_is_closed() -> None:
    """Close the response of a send that answered after the winner was chosen but before it was cancelled."""
    retry, network, gate, changes = _hedging(RetryPolicy(hedge=Hedge(), budget=None))
    request, _ = _request(clock=TestClock())

    call = asyncio.create_task(retry.handle_async_request(request))
    await _hedge_started(network, gate, changes)
    network.started[0].set_result(200)
    network.started[1].set_result(200)
    response = await call

    loser = next(other for other in network.responses if other is not response)
    assert loser.is_closed
    await response.aclose()


async def test_a_hedged_failure_waits_for_the_other_send() -> None:
    """Keep waiting on the other send when one fails retryably, and close the failure's response."""
    retry, network, gate, changes = _hedging(RetryPolicy(hedge=Hedge(), budget=None))
    request, _ = _request(clock=TestClock())

    call = asyncio.create_task(retry.handle_async_request(request))
    await _hedge_started(network, gate, changes)
    network.started[0].set_result(503)
    await changes.until(lambda: len(network.responses) == 1)
    network.started[1].set_result(200)
    response = await call

    assert response.status_code == httpx2.codes.OK
    assert network.responses[0].is_closed
    await response.aclose()


async def test_when_every_hedged_send_fails_the_retry_loop_takes_over() -> None:
    """Back off and retry after a round in which every send failed, with hedges spent from the same attempts."""
    clock = TestClock()
    retry, network = _transport(
        [Slow(seconds=1, then=Respond(503)), Respond(503), Respond(200)],
        clock,
        RetryPolicy(backoff=_FULL, budget=None, hedge=Hedge(after=_HEDGE_AFTER)),
    )
    request, context = _request(clock=clock)

    response = await retry.handle_async_request(request)

    assert response.status_code == httpx2.codes.OK
    assert len(network.calls) == context.attempt == _THREE_SENDS
    assert all(call.response_closed for call in network.calls[:2])
    await response.aclose()


async def test_an_answer_before_the_hedge_delay_cancels_the_timer() -> None:
    """Return a quick answer without hedging, cancelling the hedge timer still waiting."""
    changes = _Changes()
    gate = _Gate(changes)
    network = FaultyTransport(schedule=[Respond(200)])
    retry = RetryTransport(transport=network, policy=RetryPolicy(hedge=Hedge(), budget=None), sleep=gate.sleep)
    request, _ = _request(clock=TestClock())

    response = await retry.handle_async_request(request)

    assert len(network.calls) == 1
    assert not gate.opened.is_set()
    await response.aclose()


async def test_a_hedge_is_not_started_without_budget() -> None:
    """Skip the hedge when the budget has no room, and wait for the first send alone."""
    clock = TestClock()
    policy = RetryPolicy(hedge=Hedge(after=_HEDGE_AFTER), budget=RetryBudget(ratio=0.0, min_per_second=0.0))
    retry, network = _transport([Slow(seconds=1), Respond(200)], clock, policy)
    request, _ = _request(clock=clock)

    response = await retry.handle_async_request(request)

    assert len(network.calls) == 1
    await response.aclose()


async def test_a_hedge_is_not_started_past_the_deadline() -> None:
    """Skip the hedge when the time left once ``after`` passes is below ``min_attempt_time``.

    The first send outlasts ``after``, so the hedge timer fires while it is outstanding; a hedge sent would take the
    schedule's second step.
    """
    clock = TestClock()
    policy = RetryPolicy(hedge=Hedge(after=_HEDGE_AFTER), budget=None, min_attempt_time=1.0)
    retry, network = _transport([Slow(seconds=_HEDGE_AFTER + 0.1), Respond(200)], clock, policy)
    request, _ = _request(clock=clock, deadline=1.2)

    response = await retry.handle_async_request(request)

    assert len(network.calls) == 1
    await response.aclose()


async def test_a_non_idempotent_request_is_never_hedged() -> None:
    """Send a POST alone even under a hedging policy."""
    clock = TestClock()
    retry, network = _transport([Slow(seconds=1)], clock, RetryPolicy(hedge=Hedge(after=_HEDGE_AFTER), budget=None))
    request, _ = _request("POST", clock=clock)

    response = await retry.handle_async_request(request)

    assert len(network.calls) == 1
    await response.aclose()


async def test_a_cancelled_call_cancels_every_hedged_send() -> None:
    """Cancel the sends still running when the call itself is cancelled, recording each in the history."""
    retry, network, gate, changes = _hedging(RetryPolicy(hedge=Hedge(), budget=None))
    request, context = _request(clock=TestClock())

    call = asyncio.create_task(retry.handle_async_request(request))
    await _hedge_started(network, gate, changes)
    call.cancel()
    with pytest.raises(asyncio.CancelledError):
        await call

    assert network.cancelled == _BOTH
    assert len(_attempt_notes(context)) == _BOTH


# --- Teardown ---


class _RecordsClose(httpx2.AsyncBaseTransport):
    """A transport that records whether it was closed."""

    def __init__(self) -> None:
        """Start unclosed."""
        self.closed = False

    @override
    async def aclose(self) -> None:
        self.closed = True


async def test_retry_transport_closes_the_transport_it_wraps() -> None:
    """Delegate teardown, since httpx2's ``AsyncBaseTransport.aclose()`` is a no-op."""
    inner = _RecordsClose()

    await RetryTransport(transport=inner, policy=RetryPolicy()).aclose()

    assert inner.closed is True
