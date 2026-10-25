# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""The rate budget is spent by a transport, and where it sits in the stack is the behaviour under test.

``AsyncBaseClient`` spends its budget from inside its transport stack::

    RetryTransport( ResilientCacheTransport( RateLimitTransport( base )))

Two properties follow from that placement, which is why they are tested here
(the budget's own pacing is ``test_budget.py``'s subject):

* a cache hit short-circuits above the budget, so it spends no turn. A fully cached workload does not pace itself
  against a network it never touches;
* every re-send the retry layer makes passes through the budget, so a retried request spends one turn per attempt.
  A client cannot exceed its declared rate by retrying.

A per-path ``RateRule`` keys off the URL, which hishel's cache transport passes through as it is; request extensions,
which hishel drops on its way down, play no part in choosing the budget.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, override

import anysqlite
import httpx2
import pytest
from hishel import AsyncSqliteStorage, FilterPolicy

from headgate import HttpOptions, TransportPolicy
from headgate.cache import HttpCache, PathFragmentFilter, StatusCodeFilter
from headgate.ratelimit import PathBudgets, Rate, RateBudget, RateLimitTransport, RateRule
from headgate.retry import RetryPolicy
from tests.support.budgets import default_budget, rate_limit_transport
from tests.support.clients import StubClient
from tests.support.responses import RESPONSE

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from headgate._core import CallContext

#: Fast enough never to sleep in a test; the subject is which requests acquire, not timing.
_INSTANT = 1_000_000.0


class _CountingTransport(httpx2.AsyncBaseTransport):
    """Record requests, serve responses scripted per call, and record whether it was closed.

    Given the ``acquired`` tally (the fixture below), it also records how many turns had been taken, across every
    budget, at the moment each request reached it: the evidence for when the budget is spent relative to the
    send, which the final tally cannot show.
    """

    def __init__(self, *statuses: int, acquired: dict[int, int] | None = None) -> None:
        """Serve *statuses* in order (the last repeats), 200 when none are given."""
        self._statuses = list(statuses) or [200]
        self._acquired = acquired
        self.requests: list[httpx2.Request] = []
        self.turns_taken_on_arrival: list[int] = []
        self.closed = False

    @override
    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        if self._acquired is not None:
            self.turns_taken_on_arrival.append(sum(self._acquired.values()))
        status = self._statuses[min(len(self.requests) - 1, len(self._statuses) - 1)]
        return httpx2.Response(status, content=b'{"ok": true}', headers={"Cache-Control": "max-age=3600"})

    @override
    async def aclose(self) -> None:
        self.closed = True


@pytest.fixture
async def sqlite_storage() -> AsyncIterator[AsyncSqliteStorage]:
    """Yield an in-memory hishel SQLite storage, closed after the test."""
    conn = await anysqlite.connect(":memory:")
    storage = AsyncSqliteStorage(connection=conn)
    try:
        yield storage
    finally:
        await storage.close()


def _series_policy() -> FilterPolicy:
    """Cache 200s for ``/series`` paths only."""
    return FilterPolicy(
        request_filters=[PathFragmentFilter(("/series",))],
        response_filters=[StatusCodeFilter(frozenset({200}))],
    )


@pytest.fixture
def acquired(monkeypatch: pytest.MonkeyPatch) -> dict[int, int]:
    """Count the turns taken per budget, keyed by ``id``, through ``RateBudget.wait``, the spending seam.

    It wraps the autouse ``no_rate_limit`` no-op, so counting never sleeps.
    """
    counts: dict[int, int] = {}
    original = RateBudget.wait

    async def counting_wait(self: RateBudget, context: CallContext) -> None:
        counts[id(self)] = counts.get(id(self), 0) + 1
        await original(self, context)

    monkeypatch.setattr(RateBudget, "wait", counting_wait)
    return counts


async def test_a_cache_hit_takes_no_token(sqlite_storage: AsyncSqliteStorage, acquired: dict[int, int]) -> None:
    """Spend no turn on a response served from cache: it reached no network."""
    base = _CountingTransport()
    client = StubClient(
        base_url="https://x",
        rate=Rate.per_second(_INSTANT),
        policy=TransportPolicy(cache=HttpCache(policy=_series_policy(), storage=sqlite_storage)),
        http=HttpOptions(transport=base),
    )

    await client.get("/series?id=A", expect=RESPONSE)
    await client.get("/series?id=A", expect=RESPONSE)

    assert len(base.requests) == 1, "the second GET must be served from cache"
    assert acquired[id(default_budget(client))] == 1, "a cached response reached no network and must spend no turn"


async def test_every_retry_takes_a_token(acquired: dict[int, int]) -> None:
    """Spend one token per send, so a client cannot exceed its rate by retrying."""
    statuses = (500, 500, 200)
    base = _CountingTransport(*statuses)
    client = StubClient(
        base_url="https://x",
        rate=Rate.per_second(_INSTANT),
        policy=TransportPolicy(retry=RetryPolicy(attempts=len(statuses))),
        http=HttpOptions(transport=base),
    )

    await client.get("/thing", expect=RESPONSE)

    assert len(base.requests) == len(statuses), "two retries then success"
    assert acquired[id(default_budget(client))] == len(statuses), (
        "each re-send is a request on the wire and must spend its own turn"
    )


async def test_the_token_is_taken_before_the_request_is_sent(acquired: dict[int, int]) -> None:
    """Acquire, then send.

    The final tally cannot tell the order apart: a transport that sent first and acquired afterwards spends the same
    turns, but paces nothing, because the request is already on the wire by the time the budget is consulted. So the
    inner transport records the tally as each request reaches it.
    """
    base = _CountingTransport(acquired=acquired)
    client = StubClient(base_url="https://x", rate=Rate.per_second(_INSTANT), http=HttpOptions(transport=base))

    await client.get("/thing", expect=RESPONSE)

    assert base.turns_taken_on_arrival == [1], "the budget must already have been acquired when the request was sent"


async def test_each_retry_takes_its_token_before_it_is_sent(acquired: dict[int, int]) -> None:
    """Take each attempt's token before its send: the Nth send arrives with N turns taken, not N - 1."""
    statuses = (500, 500, 200)
    base = _CountingTransport(*statuses, acquired=acquired)
    client = StubClient(
        base_url="https://x",
        rate=Rate.per_second(_INSTANT),
        policy=TransportPolicy(retry=RetryPolicy(attempts=len(statuses))),
        http=HttpOptions(transport=base),
    )

    await client.get("/thing", expect=RESPONSE)

    assert base.turns_taken_on_arrival == list(range(1, len(statuses) + 1))


async def test_a_path_rule_selects_its_own_budget(acquired: dict[int, int]) -> None:
    """Spend a matching request's token on its rule's budget and any other request's on the default."""
    base = _CountingTransport()
    client = StubClient(
        base_url="https://x",
        rate=Rate.per_second(_INSTANT),
        policy=TransportPolicy(rate_limits=(RateRule("/candles", Rate.per_second(_INSTANT)),)),
        http=HttpOptions(transport=base),
    )

    await client.get("/products/BTC-USD/candles", expect=RESPONSE)
    await client.get("/products/BTC-USD", expect=RESPONSE)

    budgets = rate_limit_transport(client).budgets
    candles = budgets.for_request(httpx2.Request("GET", "https://x/products/BTC-USD/candles"))
    assert candles is not budgets.default
    assert (acquired[id(candles)], acquired[id(budgets.default)]) == (1, 1)


async def test_aclose_reaches_the_transport_below_the_budget() -> None:
    """Delegate teardown through the budget layer.

    httpx2's ``AsyncBaseTransport.aclose()`` is a no-op, so a budget in the middle of the stack that did not override
    it would silently strand the connection pool below it.
    """
    base = _CountingTransport()
    client = StubClient(rate=Rate.per_second(2), base_url="https://x", http=HttpOptions(transport=base))

    await client.aclose()

    assert base.closed is True


async def test_the_transport_paces_each_request_on_the_budget_its_path_selects(acquired: dict[int, int]) -> None:
    """Take a request's turn from the budget :class:`PathBudgets` selects for its path, with no client around."""
    budgets = PathBudgets(default=Rate.per_second(_INSTANT), rules=[RateRule("/candles", Rate.per_second(_INSTANT))])
    transport = RateLimitTransport(budgets=budgets, transport=_CountingTransport())
    candles = httpx2.Request("GET", "https://x/candles")

    await transport.handle_async_request(candles)

    assert transport.budgets is budgets
    assert acquired == {id(budgets.for_request(candles)): 1}
