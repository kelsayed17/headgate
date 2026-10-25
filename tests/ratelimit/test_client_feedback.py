# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""Server rate-limit feedback through ``AsyncBaseClient``: a pause every sharer honours, or raises past a deadline.

The budgets here run on a fake clock and sleep, so a pause of minutes costs nothing; the module keeps the real
``RateBudget.wait`` (``transport_policy``), which is where the pause is enforced.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import anysqlite
import httpx2
import pytest
from hishel import AsyncSqliteStorage, FilterPolicy

from headgate import HttpCache, HttpOptions, RateLimitError, TransportPolicy
from headgate._cache import CACHE_DEGRADATIONS, MethodFilter, StatusCodeFilter
from headgate._core import Event, RateLimited, Timing
from headgate.ratelimit import Rate, RateBudget
from headgate.retry import NO_RETRY
from tests.support.breakers import failures
from tests.support.clients import StubClient
from tests.support.clock import FakeClock
from tests.support.responses import RESPONSE

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

pytestmark = pytest.mark.transport_policy

_HOST = "limited.example"

#: The pause the server asks for, and a call deadline shorter than it.
_PAUSE = 30
_DEADLINE = 1.0


class _Recorder:
    """Collect every event delivered."""

    def __init__(self) -> None:
        """Start empty."""
        self.events: list[Event] = []

    def notify(self, event: Event) -> None:
        """Keep *event*."""
        self.events.append(event)


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


def _budget(clock: FakeClock) -> RateBudget:
    """Return a generous budget on *clock*."""
    return RateBudget(Rate.per_second(100), timing=Timing(clock=clock, sleep=clock.sleep), wall_clock=clock)


def _client(
    budget: RateBudget, server: _Server, *, cache: HttpCache | None = None, recorder: _Recorder | None = None
) -> StubClient:
    """Return a client spending *budget* against *server*, sending each call once."""
    return StubClient(
        base_url=f"https://{_HOST}",
        rate=budget,
        policy=TransportPolicy(retry=NO_RETRY, cache=cache),
        http=HttpOptions(transport=httpx2.MockTransport(server), observers=() if recorder is None else (recorder,)),
    )


@pytest.fixture
async def storage() -> AsyncIterator[AsyncSqliteStorage]:
    """Yield an in-memory hishel storage, closed after the test."""
    store = AsyncSqliteStorage(connection=await anysqlite.connect(":memory:"))
    try:
        yield store
    finally:
        await store.close()


async def test_a_429_pauses_the_budget_for_every_client_sharing_it() -> None:
    """Make a second client sharing the budget wait out the first client's 429 before it sends."""
    clock = FakeClock()
    budget = _budget(clock)
    server = _Server()
    recorder = _Recorder()
    with pytest.raises(RateLimitError):
        await _client(budget, server).get("x", expect=RESPONSE)

    response = await _client(budget, server, recorder=recorder).get("x", expect=RESPONSE)

    assert response.status_code == httpx2.codes.OK
    assert clock.slept == [_PAUSE]
    waits = [event for event in recorder.events if isinstance(event, RateLimited)]
    assert [(event.delay, event.reason) for event in waits] == [(_PAUSE, "paused")]


async def test_a_pause_past_the_deadline_raises_rate_limit_error_without_sending() -> None:
    """Raise ``RateLimitError`` at once, retryable and carrying the pause, when the call cannot outwait it."""
    clock = FakeClock()
    budget = _budget(clock)
    server = _Server()
    client = _client(budget, server)
    with pytest.raises(RateLimitError):
        await client.get("x", expect=RESPONSE)

    with pytest.raises(RateLimitError) as raised:
        await client.get("x", expect=RESPONSE, deadline=_DEADLINE)

    error = raised.value
    assert (error.status_code, error.retryable, error.retry_after) == (429, True, _PAUSE)
    assert "past the call's deadline" in str(error)
    assert server.received == 1, "the refused call never reached the server"
    assert failures(_HOST) == 0, "neither the 429 nor the local refusal is a host failure"


async def test_a_pause_past_the_deadline_is_not_mistaken_for_a_cache_fault(storage: AsyncSqliteStorage) -> None:
    """Raise the pause through the cache layer as itself, counting no cache degradation."""
    clock = FakeClock()
    budget = _budget(clock)
    server = _Server()
    policy = FilterPolicy(
        request_filters=[MethodFilter(frozenset({"GET"}))], response_filters=[StatusCodeFilter(frozenset({200}))]
    )
    cache = HttpCache(policy=policy, storage=storage)
    client = _client(budget, server, cache=cache)
    with pytest.raises(RateLimitError):
        await client.get("x", expect=RESPONSE)
    before = CACHE_DEGRADATIONS[_HOST]

    with pytest.raises(RateLimitError, match="past the call's deadline"):
        await client.get("x", expect=RESPONSE, deadline=_DEADLINE)

    assert CACHE_DEGRADATIONS[_HOST] == before
