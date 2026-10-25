# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""A 429 reaches both the rate budget, which pauses every call sharing it, and the retry layer, which waits it.

Both read the response's ``Retry-After`` the same way, so the retry's wait and the budget's pause end together and
the call waits once. The budget's real wait is kept (``transport_policy``) and runs on a test clock.
"""

from __future__ import annotations

import httpx2
import pytest

from headgate import HttpOptions, RateBudget, nothing
from headgate.events import RateLimited, RetryScheduled
from headgate.ratelimit import Rate
from headgate.testing import FakeTransport, TestClock
from tests.integration._support import client
from tests.support.events import Recorder

pytestmark = pytest.mark.transport_policy

#: The seconds the server asks for, in whole seconds and in milliseconds.
_ASKED = 2.0
_ASKED_MS = 1500


async def _waits(limited: httpx2.Response) -> tuple[TestClock, Recorder]:
    """Make one call answered *limited* and then 200, on a test clock; return the clock and the call's events."""
    clock = TestClock()
    recorder = Recorder()
    budget = RateBudget(Rate.per_second(100), timing=clock.timing, wall_clock=clock)
    fake = FakeTransport().route("GET", "/v1/x", limited, 200)
    with clock.install():
        built = client(fake, rate=budget, http=HttpOptions(observers=(recorder,)))

    await built.get("x", expect=nothing)

    return clock, recorder


async def test_a_429_is_waited_once_and_reported_as_rate_limiting() -> None:
    """Wait the ``Retry-After`` once, in the retry layer, and report the pause the 429 put on the budget."""
    clock, recorder = await _waits(httpx2.Response(429, headers={"Retry-After": f"{_ASKED:g}"}))

    assert clock.sleeps == [_ASKED], "the budget's pause had ended by the time the retry was sent"
    assert [(event.delay, event.reason) for event in recorder.of(RateLimited)] == [(_ASKED, "pushback")]
    assert [event.delay for event in recorder.of(RetryScheduled)] == [_ASKED]


async def test_the_budget_pauses_for_exactly_the_wait_the_retry_makes() -> None:
    """Pause the budget for ``retry-after-ms`` too, so it does not keep the retry waiting for the coarser header."""
    asked = _ASKED_MS / 1000
    limited = httpx2.Response(429, headers={"retry-after-ms": str(_ASKED_MS), "Retry-After": f"{_ASKED:g}"})

    clock, recorder = await _waits(limited)

    assert clock.sleeps == [asked]
    assert [(event.delay, event.reason) for event in recorder.of(RateLimited)] == [(asked, "pushback")]
