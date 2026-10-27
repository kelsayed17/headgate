# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""A ``with_options`` view shares its client's retry budget, rate budget and circuit breakers.

The view is built by copying the client, and each of the three lives in a different place (the retry transport's
ledgers, the rate-limit transport's budgets, the breaker registry), so each is checked by behaviour: what a call
through one spends or trips, a call through the other finds spent or tripped.
"""

from __future__ import annotations

import httpx2
import pytest

from headgate import (
    BreakerPolicy,
    CircuitOpenError,
    HttpOptions,
    RateBudget,
    RateLimitError,
    RetryPolicy,
    TransportPolicy,
    UpstreamStatusError,
    nothing,
)
from headgate.events import RateLimited
from headgate.ratelimit import Rate
from headgate.retry import NO_RETRY, RetryBudget
from headgate.testing import FakeTransport, TestClock, isolated_breakers
from tests.integration._support import client
from tests.support.events import Recorder

#: A budget that admits exactly one retry per window, whatever the traffic: no ratio, a floor of one.
_ONE_RETRY = RetryBudget(ratio=0.0, min_per_second=0.1, window=10.0)

#: The pause a 429 asks for.
_PAUSE = 5.0


async def test_a_view_spends_its_clients_retry_budget() -> None:
    """Find the one retry the budget allows already spent by the client, so the view's 503 is not retried."""
    fake = FakeTransport().route("GET", "/v1/client", 503, 200).route("GET", "/v1/view", 503, 200)
    built = client(fake, policy=TransportPolicy(retry=RetryPolicy(budget=_ONE_RETRY)))
    view = built.with_options(headers={"X-View": "1"})

    await built.get("client", expect=nothing)
    with pytest.raises(UpstreamStatusError) as raised:
        await view.get("view", expect=nothing)

    assert len(fake.calls_to("GET", "/v1/view")) == 1
    assert "retry budget exhausted" in raised.value.__notes__


@pytest.mark.transport_policy
async def test_a_view_waits_out_a_pause_its_client_was_asked_for() -> None:
    """Make the view wait the pause the client's 429 put on the shared rate budget, before it sends."""
    clock = TestClock()
    recorder = Recorder()
    limited = httpx2.Response(429, headers={"Retry-After": f"{_PAUSE:g}"})
    fake = FakeTransport().route("GET", "/v1/client", limited).route("GET", "/v1/view", 200)
    budget = RateBudget(Rate.per_second(100), timing=clock.timing, wall_clock=clock)
    with clock.install():
        built = client(
            fake, rate=budget, policy=TransportPolicy(retry=NO_RETRY), http=HttpOptions(observers=(recorder,))
        )
    view = built.with_options(deadline=None)

    with pytest.raises(RateLimitError):
        await built.get("client", expect=nothing)
    await view.get("view", expect=nothing)

    assert clock.sleeps == [_PAUSE]
    assert [(event.delay, event.reason) for event in recorder.of(RateLimited)] == [
        (_PAUSE, "pushback"),
        (_PAUSE, "paused"),
    ]


async def test_a_view_is_refused_by_a_breaker_its_client_opened() -> None:
    """Refuse the view's call without sending it once the client's failure opened the origin's breaker."""
    fake = FakeTransport().route("GET", "/v1/x", 503, 200)
    policy = TransportPolicy(retry=NO_RETRY, breaker=BreakerPolicy(failure_threshold=1))
    with isolated_breakers():
        built = client(fake, policy=policy)
    view = built.with_options(retry=True)

    with pytest.raises(UpstreamStatusError):
        await built.get("x", expect=nothing)
    with pytest.raises(CircuitOpenError):
        await view.get("x", expect=nothing)

    assert len(fake.calls) == 1
