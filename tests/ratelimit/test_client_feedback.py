# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""Server rate-limit feedback through ``Client``: a pause every sharer honors, or raises past a deadline.

The budgets here run on a fake clock and sleep, so a pause of minutes costs nothing; the module keeps the real
``RateBudget.wait`` (``call_policy``), which is where the pause is enforced.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx2
import pytest

from headgate import CallPolicy, Client, HttpCache, HttpOptions, Rate, RateBudget, RateLimitError
from headgate._core import AttemptFinished, CallFinished, RateLimited, Timing
from headgate.events import CacheChecked, CacheOutcome
from headgate.testing import TestClock
from tests.support.breakers import failures
from tests.support.events import Recorder
from tests.support.responses import RESPONSE
from tests.support.timing import built_on

if TYPE_CHECKING:
    from hishel import AsyncSqliteStorage

pytestmark = pytest.mark.call_policy

_HOST = "limited.example"

#: The pause the server asks for, and a call deadline shorter than it.
_PAUSE = 30
_DEADLINE = 1.0


class _Server:
    """Refuse the first request with a 429 asking for :data:`_PAUSE` seconds, then answer 200; count the requests."""

    def __init__(self) -> None:
        """Start with nothing received."""
        self.received = 0

    def __call__(self, _request: httpx2.Request) -> httpx2.Response:
        """Answer the request."""
        self.received += 1
        if self.received == 1:
            return httpx2.Response(429, headers={"Retry-After": str(_PAUSE)})
        return httpx2.Response(200, content=b"{}")


#: The timing each budget :func:`_budget` built runs on, which the clients sharing it are built on too.
_TIMINGS: dict[RateBudget, Timing] = {}


def _budget(clock: TestClock) -> RateBudget:
    """Return a generous budget on *clock*."""
    timing = Timing(clock=clock, sleep=clock.sleep, wall_clock=clock)
    budget = built_on(timing, lambda: RateBudget(Rate.per_second(100)))
    _TIMINGS[budget] = timing
    return budget


def _client(
    budget: RateBudget, server: _Server, *, cache: HttpCache | None = None, recorder: Recorder | None = None
) -> Client:
    """Return a client spending *budget* against *server*, sending each call once, on the budget's timing."""
    return built_on(
        _TIMINGS[budget],
        lambda: Client(
            base_url=f"https://{_HOST}",
            policy=CallPolicy(rate=budget, retry=None, cache=cache),
            http=HttpOptions(transport=httpx2.MockTransport(server)),
            observers=() if recorder is None else (recorder,),
        ),
    )


async def test_a_429_pauses_the_budget_for_every_client_sharing_it() -> None:
    """Make a second client sharing the budget wait out the first client's 429 before it sends."""
    clock = TestClock()
    budget = _budget(clock)
    server = _Server()
    recorder = Recorder()
    with pytest.raises(RateLimitError):
        await _client(budget, server).get("x", expect=RESPONSE)

    response = await _client(budget, server, recorder=recorder).get("x", expect=RESPONSE)

    assert response.status_code == httpx2.codes.OK
    assert clock.sleeps == [_PAUSE]
    waits = [event for event in recorder.events if isinstance(event, RateLimited)]
    assert [(event.delay, event.reason) for event in waits] == [(_PAUSE, "paused")]


async def test_a_pause_past_the_deadline_raises_rate_limit_error_without_sending() -> None:
    """Raise ``RateLimitError`` at once, retryable and carrying the pause, when the call cannot outwait it."""
    clock = TestClock()
    budget = _budget(clock)
    server = _Server()
    client = _client(budget, server)
    with pytest.raises(RateLimitError) as refused:
        await client.get("x", expect=RESPONSE)

    with pytest.raises(RateLimitError) as raised:
        await client.get("x", expect=RESPONSE, deadline=_DEADLINE)

    assert refused.value.status_code == httpx2.codes.TOO_MANY_REQUESTS, "the 429 the wire sent for that call"
    error = raised.value
    assert (error.status_code, error.retryable, error.retry_after, error.sent) == (None, True, _PAUSE, False)
    assert (error.attempts, getattr(error, "__notes__", [])) == (0, []), "an attempt never sent is no attempt"
    assert str(error).endswith("past the call's deadline"), "no status tag: this call got no status"
    assert server.received == 1, "the refused call never reached the server"
    assert failures(client, _HOST) == 0, "neither the 429 nor the local refusal is a host failure"


async def test_a_call_the_paused_budget_refuses_reports_no_attempt() -> None:
    """Emit no ``AttemptFinished`` for a send the budget refused before it reached the network."""
    clock = TestClock()
    budget = _budget(clock)
    server = _Server()
    recorder = Recorder()
    with pytest.raises(RateLimitError):
        await _client(budget, server).get("x", expect=RESPONSE)

    with pytest.raises(RateLimitError):
        await _client(budget, server, recorder=recorder).get("x", expect=RESPONSE, deadline=_DEADLINE)

    assert not [event for event in recorder.events if isinstance(event, AttemptFinished)]
    finished = [event for event in recorder.events if isinstance(event, CallFinished)]
    assert [(event.attempts, event.error) for event in finished] == [(0, "RateLimitError")]


async def test_a_pause_past_the_deadline_is_not_mistaken_for_a_cache_fault(storage: AsyncSqliteStorage) -> None:
    """Raise the pause through the cache layer as itself, reporting no cache degradation."""
    clock = TestClock()
    budget = _budget(clock)
    server = _Server()
    cache = HttpCache(storage=storage)
    recorder = Recorder()
    client = _client(budget, server, cache=cache, recorder=recorder)
    with pytest.raises(RateLimitError):
        await client.get("x", expect=RESPONSE)

    with pytest.raises(RateLimitError, match="past the call's deadline"):
        await client.get("x", expect=RESPONSE, deadline=_DEADLINE)

    assert CacheOutcome.DEGRADED not in [event.outcome for event in recorder.of(CacheChecked)]


async def test_feedback_from_another_origin_neither_pauses_nor_throttles_the_shared_budget() -> None:
    """Pace a call to another origin, but heed only the client's own origin's 429: the budget is shared by origin."""
    clock = TestClock()
    budget = _budget(clock)
    client = _client(budget, _Server())
    with pytest.raises(RateLimitError):
        await client.get(httpx2.URL("https://elsewhere.example/x"), expect=RESPONSE)
    assert (budget.pause_remaining, budget.effective_rate) == (0.0, budget.rate)

    own = _client(budget, _Server())
    with pytest.raises(RateLimitError):
        await own.get("x", expect=RESPONSE)
    assert budget.pause_remaining == _PAUSE
