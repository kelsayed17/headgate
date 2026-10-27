# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""Hedged sends spend the retry budget like any retry, and the breaker sees a hedged call once.

A hedge is a send the retry layer starts beside a slow one, inside one logical call; the breaker scope spans the whole
call, so however many of its sends fail, the origin is charged one failure for it.
"""

from __future__ import annotations

import asyncio
import itertools

import httpx2
import pytest

from headgate import BreakerPolicy, ClientError, HttpOptions, RetryPolicy, TransportPolicy, nothing
from headgate.breaker import BreakerState
from headgate.events import RetryScheduled
from headgate.retry import Hedge, RetryBudget
from headgate.testing import FakeTransport, TestClock, isolated_breakers
from tests.integration._support import BASE, client
from tests.support.events import Recorder

_HEDGE = Hedge(after=0.5)

#: A budget that admits exactly one retry or hedge per window, whatever the traffic.
_ONE_RETRY = RetryBudget(ratio=0.0, min_per_second=0.1, window=10.0)

#: How long a slow first send takes, on the test clock: longer than the hedge's ``after``.
_SLOW = 1.0


async def test_a_hedge_spends_the_retry_budget() -> None:
    """Hedge the first call with the budget's one retry, and leave the second call to its single slow send."""
    clock = TestClock()
    recorder = Recorder()
    sent = itertools.count(1)

    async def first_hangs_then_slow(_request: httpx2.Request) -> httpx2.Response:
        number = next(sent)
        if number == 1:
            await asyncio.Event().wait()
        if number == len(("hung", "hedge", "slow")):
            await clock.sleep(_SLOW)
        return httpx2.Response(200)

    fake = FakeTransport().route("GET", "/v1/x", first_hangs_then_slow)
    policy = TransportPolicy(retry=RetryPolicy(hedge=_HEDGE, budget=_ONE_RETRY))
    with clock.install():
        built = client(fake, policy=policy, http=HttpOptions(observers=(recorder,)))

    await built.get("x", expect=nothing)
    await built.get("x", expect=nothing)

    assert len(fake.calls) == len(("hung", "hedge", "slow")), "the second call was not hedged"
    assert [event.reason for event in recorder.of(RetryScheduled)] == ["hedge"]


async def test_a_hedged_call_is_charged_to_the_breaker_once() -> None:
    """Charge one failure for a call whose first send and hedge both failed, not one per send."""
    hedge_sent = asyncio.Event()

    async def first_fails_after_the_hedge(request: httpx2.Request) -> httpx2.Response:
        await hedge_sent.wait()
        msg = "connection reset mid-response"
        raise httpx2.ReadError(msg, request=request)

    def hedge_unavailable(_request: httpx2.Request) -> httpx2.Response:
        hedge_sent.set()
        return httpx2.Response(503)

    clock = TestClock()
    fake = FakeTransport().route("GET", "/v1/x", first_fails_after_the_hedge, hedge_unavailable)
    policy = TransportPolicy(
        retry=RetryPolicy(attempts=2, hedge=_HEDGE), breaker=BreakerPolicy(failure_threshold=len(("a", "b")))
    )
    with clock.install(), isolated_breakers() as registry:
        built = client(fake, policy=policy)

    with pytest.raises(ClientError, match="hedge"):
        await built.get("x", expect=nothing)

    (breaker,) = registry.breakers(BASE)
    assert len(fake.calls) == len(("first", "hedge"))
    assert (breaker.state, breaker.failure_count) == (BreakerState.CLOSED, 1)


async def test_a_hedge_losers_failure_is_not_charged_when_the_call_succeeds() -> None:
    """Record a success for a call whose hedge answered, though its first send then failed."""
    hedge_sent = asyncio.Event()

    async def first_fails_after_the_hedge(request: httpx2.Request) -> httpx2.Response:
        await hedge_sent.wait()
        msg = "connection reset mid-response"
        raise httpx2.ReadError(msg, request=request)

    def hedge_answers(_request: httpx2.Request) -> httpx2.Response:
        hedge_sent.set()
        return httpx2.Response(200)

    clock = TestClock()
    fake = FakeTransport().route("GET", "/v1/x", first_fails_after_the_hedge, hedge_answers)
    policy = TransportPolicy(retry=RetryPolicy(hedge=_HEDGE), breaker=BreakerPolicy(failure_threshold=1))
    with clock.install(), isolated_breakers() as registry:
        built = client(fake, policy=policy)

    await built.get("x", expect=nothing)

    (breaker,) = registry.breakers(BASE)
    assert (breaker.state, breaker.failure_count) == (BreakerState.CLOSED, 0)
