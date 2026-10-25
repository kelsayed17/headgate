# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""The client puts ``BREAKER_GATE`` on every call, and the retry layer reads it before each further attempt.

The breaker counts a call once, when it ends, so what opens it in the middle of a call's retry sequence is another
call failing against the same origin; here that other call is made from inside the first attempt's answer.
"""

from __future__ import annotations

import httpx2
import pytest

from headgate import BreakerPolicy, HttpOptions, TransportPolicy, UpstreamStatusError, nothing
from headgate.breaker import BreakerState, CircuitBreaker
from headgate.events import BreakerChanged
from headgate.testing import FakeTransport, TestClock, isolated_breakers
from tests.integration._support import BASE, Recorder, client

_PATH = "/v1/x"
_COOLDOWN = 60.0
_POLICY = TransportPolicy(breaker=BreakerPolicy(failure_threshold=1, recovery_timeout=_COOLDOWN))


def _fail_once(breaker: CircuitBreaker) -> None:
    """Record one failure on *breaker*, as a concurrent call to its origin that failed would."""
    with breaker.track() as verdict:
        verdict.mark_failed()


async def test_a_breaker_opening_mid_sequence_stops_the_retries() -> None:
    """Stop re-sending once the origin's breaker opens, raising the last failure with a ``circuit opened`` note."""
    breakers: list[CircuitBreaker] = []

    def unavailable_while_another_call_fails(_request: httpx2.Request) -> httpx2.Response:
        _fail_once(breakers[0])
        return httpx2.Response(503)

    fake = FakeTransport().route("GET", _PATH, unavailable_while_another_call_fails, 200)
    with isolated_breakers() as registry:
        built = client(fake, policy=_POLICY)
    breakers.append(registry.breaker_for(BASE, failure_threshold=1, recovery_timeout=_COOLDOWN))

    with pytest.raises(UpstreamStatusError) as raised:
        await built.get("x", expect=nothing)

    assert len(fake.calls) == 1, "no attempt after the breaker opened"
    assert "circuit opened" in raised.value.__notes__
    assert breakers[0].state is BreakerState.OPEN
    assert breakers[0].failure_count == 1, "the call admitted while closed changes nothing once the breaker tripped"


async def test_a_half_open_probe_keeps_its_retries() -> None:
    """Let the half-open breaker's one probe use its whole retry sequence, and close the breaker when it succeeds."""
    clock = TestClock()
    recorder = Recorder()
    fake = FakeTransport().route("GET", _PATH, 503, 502, 200)
    with clock.install(), isolated_breakers() as registry:
        built = client(fake, policy=_POLICY, http=HttpOptions(observers=(recorder,)))
    breaker = registry.breaker_for(BASE, failure_threshold=1, recovery_timeout=_COOLDOWN)
    _fail_once(breaker)
    clock.advance(_COOLDOWN)

    await built.get("x", expect=nothing)

    assert len(fake.calls) == len((503, 502, 200))
    assert breaker.state is BreakerState.CLOSED
    moves = [(event.previous, event.state) for event in recorder.of(BreakerChanged)]
    assert moves == [(BreakerState.OPEN, BreakerState.HALF_OPEN), (BreakerState.HALF_OPEN, BreakerState.CLOSED)]
