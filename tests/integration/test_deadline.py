# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""One deadline bounds a call across every layer: the rate budget's waits, the retry layer's backoff and each attempt.

Each layer reads the time left from the call's context, so none starts a wait or a send the deadline cannot cover; and
the client's ``asyncio.timeout`` cancels whatever is still running when it passes, raising ``ClientTimeoutError``.
"""

from __future__ import annotations

import asyncio

import httpx2
import pytest

from headgate import (
    ClientTimeoutError,
    RateBudget,
    RateLimitError,
    RetryPolicy,
    TransportPolicy,
    UpstreamStatusError,
    nothing,
)
from headgate.ratelimit import Rate
from headgate.retry import NO_RETRY, Backoff
from headgate.testing import FakeTransport, TestClock
from tests.integration._support import client

#: The server's pause, the backoff before the first retry, and a deadline that covers the pause but not both.
_PAUSE = 1.0
_BACKOFF = 2.0
_DEADLINE = 2.5

#: A deadline short enough to pass in real time while an attempt hangs.
_SHORT_DEADLINE = 0.2


def _full_jitter() -> float:
    """Return the top of the jitter range, so the backoff waits its nominal value."""
    return 1.0


@pytest.mark.transport_policy
async def test_a_pause_a_backoff_and_attempts_stay_inside_the_deadline() -> None:
    """Wait the pause and send, then stop rather than start a backoff the deadline cannot cover."""
    clock = TestClock()
    budget = RateBudget(Rate.per_second(100), timing=clock.timing, wall_clock=clock)
    timeouts: list[float] = []

    def unavailable(request: httpx2.Request) -> httpx2.Response:
        timeouts.append(request.extensions["timeout"]["read"])
        return httpx2.Response(503)

    limited = httpx2.Response(429, headers={"Retry-After": f"{_PAUSE:g}"})
    fake = FakeTransport().route("GET", "/v1/limited", limited).route("GET", "/v1/x", unavailable)
    retry = RetryPolicy(backoff=Backoff(factor=_BACKOFF / 2, max_wait=_BACKOFF, jitter=_full_jitter))
    with clock.install():
        built = client(fake, rate=budget, policy=TransportPolicy(retry=retry))
    with pytest.raises(RateLimitError):
        await built.get("limited", expect=nothing, retry=NO_RETRY)
    started = clock.now

    with pytest.raises(UpstreamStatusError) as raised:
        await built.get("x", expect=nothing, deadline=_DEADLINE)

    assert clock.now - started == _PAUSE, "the pause was waited, and the backoff never started"
    assert clock.now - started <= _DEADLINE
    assert timeouts == [_DEADLINE - _PAUSE], "the attempt's read timeout is cut to the time left"
    assert raised.value.__notes__[-1] == "deadline exhausted"


async def test_an_attempt_still_running_at_the_deadline_raises_client_timeout_error() -> None:
    """Cancel the hanging retry when the deadline passes, and name every attempt in the error's notes."""

    async def hangs(_request: httpx2.Request) -> httpx2.Response:
        await asyncio.Event().wait()
        raise AssertionError

    fake = FakeTransport().route("GET", "/v1/x", 503, hangs)
    built = client(fake, policy=TransportPolicy(retry=RetryPolicy(backoff=Backoff(factor=0.0))))

    with pytest.raises(ClientTimeoutError, match="deadline exceeded") as raised:
        await built.get("x", expect=nothing, deadline=_SHORT_DEADLINE)

    first, second = raised.value.__notes__
    assert first.startswith("attempt 1/3: 503 Service Unavailable after ")
    assert second.startswith("attempt 2/3: cancelled after ")
    assert raised.value.attempts == len((first, second))


#: Calls sent at once on a budget of one send every 0.1 s, under a deadline that admits the turns of the first three.
_QUEUED = 6
_ADMITTED = 3
_QUEUE_DEADLINE = 0.25


@pytest.mark.transport_policy
async def test_a_call_whose_budget_turn_is_past_the_deadline_fails_at_once_unsent() -> None:
    """Fail a call at once, saying it was never sent, when its turn in the rate budget comes after its deadline.

    On the real clock: the turns are 0.1 s apart, so the fourth call's turn, 0.3 s away, is past its 0.25 s deadline.
    """
    budget = RateBudget(Rate(requests=1, period=0.1), adaptive=None)
    fake = FakeTransport().route("GET", "/v1/x", 200)
    built = client(fake, rate=budget, policy=TransportPolicy(deadline=_QUEUE_DEADLINE))

    results = await asyncio.gather(*(built.get("x", expect=nothing) for _ in range(_QUEUED)), return_exceptions=True)

    assert results[:_ADMITTED] == [None] * _ADMITTED
    for error in results[_ADMITTED:]:
        assert isinstance(error, ClientTimeoutError), error
        assert "never sent" in str(error)
        assert error.attempts == 0
        assert error.retryable
        assert error.retry_after is not None
        assert error.retry_after > _QUEUE_DEADLINE
    assert len(fake.calls) == _ADMITTED


async def test_a_deadline_passing_before_the_send_says_so() -> None:
    """Report a call that timed out waiting for a concurrency slot as never sent, with no attempts made."""
    holding = asyncio.Event()

    async def hangs(_request: httpx2.Request) -> httpx2.Response:
        holding.set()
        await asyncio.Event().wait()
        raise AssertionError

    fake = FakeTransport().route("GET", "/v1/slow", hangs).route("GET", "/v1/x", 200)
    built = client(fake, rate=RateBudget(Rate.per_second(100), max_in_flight=1))
    holder = asyncio.create_task(built.get("slow", expect=nothing, retry=NO_RETRY))
    try:
        await holding.wait()
        with pytest.raises(ClientTimeoutError, match="before attempt 1 was sent") as raised:
            await built.get("x", expect=nothing, deadline=_SHORT_DEADLINE)
    finally:
        holder.cancel()
    assert raised.value.attempts == 0
