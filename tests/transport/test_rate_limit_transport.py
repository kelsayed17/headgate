"""The rate limiter is a TRANSPORT, and where it sits in the stack is the behaviour under test.

``AsyncBaseClient`` paces requests from inside its transport stack::

    RetryTransport( ResilientCacheTransport( RateLimitTransport( base )))

Two properties follow from that placement, which is why they are tested here rather than in ``test_rate_limiter.py``
(the limiter's own pacing is that file's subject):

* a cache HIT short-circuits above the limiter, so it spends no token. A fully cached workload does not pace itself
  against a network it never touches;
* every re-send the retry layer makes passes THROUGH the limiter, so a retried request spends one token per attempt.
  A client cannot exceed its declared rate by retrying.

A per-path ``RateRule`` keys off the URL, which hishel's cache transport passes through as it is; request extensions,
which hishel drops on its way down, play no part in choosing the limiter.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, override

import anysqlite
import httpx2
import pytest
from hishel import AsyncSqliteStorage, FilterPolicy

from headgate._cache import HttpCache, PathFragmentFilter, StatusCodeFilter
from headgate._rate_limit import AsyncRateLimiter, Rate, RateRule
from tests.support.clients import StubClient
from tests.support.limiters import default_limiter, rate_limit_transport
from tests.support.responses import RESPONSE

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

# Fast enough never to sleep in a test; the subject is WHICH requests acquire, not timing.
_INSTANT = 1_000_000.0


class _CountingTransport(httpx2.AsyncBaseTransport):
    """Records requests, responses scripted per call, and whether it was closed.

    Given the ``acquired`` tally (the fixture below), it also records how many tokens had been taken, across every
    limiter, at the moment each request reached it: the evidence for WHEN the limiter is acquired relative to the
    send, which the final tally cannot show.
    """

    def __init__(self, *statuses: int, acquired: dict[int, int] | None = None) -> None:
        self._statuses = list(statuses) or [200]
        self._acquired = acquired
        self.requests: list[httpx2.Request] = []
        self.tokens_taken_on_arrival: list[int] = []
        self.closed = False

    @override
    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        if self._acquired is not None:
            self.tokens_taken_on_arrival.append(sum(self._acquired.values()))
        status = self._statuses[min(len(self.requests) - 1, len(self._statuses) - 1)]
        return httpx2.Response(status, content=b'{"ok": true}', headers={"Cache-Control": "max-age=3600"})

    @override
    async def aclose(self) -> None:
        self.closed = True


@pytest.fixture
async def sqlite_storage() -> AsyncIterator[AsyncSqliteStorage]:
    """An in-memory hishel SQLite storage."""
    conn = await anysqlite.connect(":memory:")
    storage = AsyncSqliteStorage(connection=conn)
    try:
        yield storage
    finally:
        await storage.close()


def _series_policy() -> FilterPolicy:
    return FilterPolicy(
        request_filters=[PathFragmentFilter(("/series",))],
        response_filters=[StatusCodeFilter(frozenset({200}))],
    )


@pytest.fixture
def acquired(monkeypatch: pytest.MonkeyPatch) -> dict[int, int]:
    """Tokens taken per limiter (keyed by ``id``). ``AsyncRateLimiter.wait`` is the seam every acquisition passes.

    It wraps the autouse ``no_rate_limit`` no-op, so counting never sleeps.
    """
    counts: dict[int, int] = {}
    original = AsyncRateLimiter.wait

    async def counting_wait(self: AsyncRateLimiter) -> None:
        counts[id(self)] = counts.get(id(self), 0) + 1
        await original(self)

    monkeypatch.setattr(AsyncRateLimiter, "wait", counting_wait)
    return counts


async def test_a_cache_hit_takes_no_token(sqlite_storage: AsyncSqliteStorage, acquired: dict[int, int]) -> None:
    base = _CountingTransport()
    client = StubClient(
        base_url="https://x",
        transport=base,
        rate=Rate.per_second(_INSTANT),
        cache=HttpCache(policy=_series_policy(), storage=sqlite_storage),
    )

    await client.get("/series?id=A", expect=RESPONSE)
    await client.get("/series?id=A", expect=RESPONSE)

    assert len(base.requests) == 1, "the second GET must be served from cache"
    assert acquired[id(default_limiter(client))] == 1, "a cached response reached no network and must spend no token"


async def test_every_retry_takes_a_token(acquired: dict[int, int]) -> None:
    base = _CountingTransport(500, 500, 200)
    client = StubClient(base_url="https://x", transport=base, rate=Rate.per_second(_INSTANT), max_attempts=3)

    await client.get("/thing", expect=RESPONSE)

    assert len(base.requests) == 3, "two retries then success"
    assert acquired[id(default_limiter(client))] == 3, (
        "each re-send is a request on the wire and must spend its own token"
    )


async def test_the_token_is_taken_before_the_request_is_sent(acquired: dict[int, int]) -> None:
    """Acquire, THEN send. The final tally cannot tell the order apart: a transport that sent first and acquired
    afterwards spends the same tokens, but paces nothing, because the request is already on the wire by the time
    the limiter is consulted. So the inner transport records the tally as each request reaches it."""
    base = _CountingTransport(acquired=acquired)
    client = StubClient(base_url="https://x", transport=base, rate=Rate.per_second(_INSTANT))

    await client.get("/thing", expect=RESPONSE)

    assert base.tokens_taken_on_arrival == [1], "the limiter must already have been acquired when the request was sent"


async def test_each_retry_takes_its_token_before_it_is_sent(acquired: dict[int, int]) -> None:
    """The same ordering per attempt: the Nth send arrives with N tokens taken, not N - 1."""
    base = _CountingTransport(500, 500, 200, acquired=acquired)
    client = StubClient(base_url="https://x", transport=base, rate=Rate.per_second(_INSTANT), max_attempts=3)

    await client.get("/thing", expect=RESPONSE)

    assert base.tokens_taken_on_arrival == [1, 2, 3]


async def test_a_path_rule_selects_its_own_limiter(acquired: dict[int, int]) -> None:
    base = _CountingTransport()
    client = StubClient(
        base_url="https://x",
        transport=base,
        rate=Rate.per_second(_INSTANT),
        rate_limits=[RateRule("/candles", Rate.per_second(_INSTANT))],
    )

    await client.get("/products/BTC-USD/candles", expect=RESPONSE)
    await client.get("/products/BTC-USD", expect=RESPONSE)

    limiters = rate_limit_transport(client)._limiters
    candles = limiters.for_request(httpx2.Request("GET", "https://x/products/BTC-USD/candles"))
    assert candles is not limiters.default
    assert (acquired[id(candles)], acquired[id(limiters.default)]) == (1, 1)


async def test_aclose_reaches_the_transport_below_the_limiter() -> None:
    """The limiter layer must delegate teardown.

    httpx2's ``AsyncBaseTransport.aclose()`` is a no-op, so a limiter in the middle of the stack that did not override
    it would silently strand the connection pool below it.
    """
    base = _CountingTransport()
    client = StubClient(rate=Rate.per_second(2), base_url="https://x", transport=base)

    await client.aclose()

    assert base.closed is True
