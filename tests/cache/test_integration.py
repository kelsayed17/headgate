"""``AsyncBaseClient``'s cache: where it sits in the transport stack, HIT/MISS behaviour, and TTL threading.

The cache is opt-in per client (``cache=HttpCache(policy=, storage=, ttl=)``) and sits below retry and above the rate
limiter: ``RetryTransport(AsyncCacheTransport(RateLimitTransport(base)))``. A client with no ``cache``, or an
``HttpCache`` with no ``storage``, builds the plain stack with no ``AsyncCacheTransport`` at all. The ``HttpCache``'s
``ttl`` is stamped on every request as the ``hishel_ttl`` extension by the cache transport (the ``FilterPolicy``, not
the TTL, decides what is stored), and a per-call ``cache_ttl=`` wins over it.
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime
from typing import Any, override
from unittest.mock import MagicMock

import anysqlite
import httpx2
import pytest
import time_machine
from hishel import AsyncSqliteStorage
from hishel.httpx2 import AsyncCacheTransport

from headgate._cache import HttpCache, _RestoreRequestExtensions
from headgate._client import AsyncBaseClient
from headgate._rate_limit import UNLIMITED, Rate, RateLimitTransport
from headgate._retry import RetryTransport
from tests.cache._helpers import series_policy
from tests.support.clients import StubClient
from tests.support.responses import RESPONSE


def _base_of(client: AsyncBaseClient) -> httpx2.AsyncBaseTransport:
    """The transport directly below the retry layer (the cache transport, or the limiter)."""
    inner = client._http_client._transport
    assert isinstance(inner, RetryTransport)
    below = inner._transport
    assert below is not None
    return below


def _network_of(client: AsyncBaseClient) -> httpx2.AsyncBaseTransport:
    """The NETWORK transport, below every policy layer.

    A cache-enabled client carries one more layer than its shape suggests: the extension
    restorer that puts back what hishel drops (httpx2's ``timeout`` among it), which sits
    between the cache and the limiter.
    """
    below = _base_of(client)
    if isinstance(below, AsyncCacheTransport):
        below = below.next_transport
        assert isinstance(below, _RestoreRequestExtensions), "the cache must not strip the timeout"
        below = below._inner
    assert isinstance(below, RateLimitTransport), "rate limiting is the bottom transport"
    return below._transport


def test_no_cache_builds_plain_stack() -> None:
    """A client without a ``cache`` builds ``RetryTransport(RateLimitTransport(base))``, with no cache transport."""
    base = httpx2.MockTransport(lambda r: httpx2.Response(200))
    client = StubClient(rate=Rate.per_second(2), base_url="https://x", transport=base)

    below_retry = _base_of(client)
    assert not isinstance(below_retry, AsyncCacheTransport)
    assert _network_of(client) is base


def test_cache_without_storage_builds_plain_stack() -> None:
    """An ``HttpCache`` whose storage is ``None`` builds the plain stack."""
    base = httpx2.MockTransport(lambda r: httpx2.Response(200))
    client = StubClient(
        base_url="https://x", transport=base, rate=UNLIMITED, cache=HttpCache(policy=series_policy(), storage=None)
    )

    below_retry = _base_of(client)
    assert not isinstance(below_retry, AsyncCacheTransport)
    assert _network_of(client) is base


def test_cache_with_storage_inserts_cache_transport_above_the_limiter() -> None:
    """An ``HttpCache`` with a storage builds ``RetryTransport(AsyncCacheTransport(RateLimitTransport(base)))``."""
    storage = MagicMock(name="storage")
    base = httpx2.MockTransport(lambda r: httpx2.Response(200))
    client = StubClient(
        base_url="https://x",
        transport=base,
        rate=UNLIMITED,
        cache=HttpCache(policy=series_policy(), storage=storage),
    )

    cache_transport = _base_of(client)
    assert isinstance(cache_transport, AsyncCacheTransport)
    assert _network_of(client) is base
    assert cache_transport.storage is storage


# --- HIT / MISS behaviour and hishel_ttl threading, over real SQLite storage ---


class _CountingTransport(httpx2.AsyncBaseTransport):
    """The network leg: counts the requests that reach it and records each one's extensions."""

    def __init__(self) -> None:
        self.count = 0
        self.extensions: list[dict[str, Any]] = []
        self._mock = httpx2.MockTransport(self._handle)

    def _handle(self, request: httpx2.Request) -> httpx2.Response:
        self.count += 1
        self.extensions.append(dict(request.extensions))
        return httpx2.Response(200, json={"call": self.count})

    @override
    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        return await self._mock.handle_async_request(request)


async def test_cache_hit_second_call_no_network(storage: AsyncSqliteStorage) -> None:
    base = _CountingTransport()
    client = StubClient(
        base_url="https://x",
        transport=base,
        rate=UNLIMITED,
        cache=HttpCache(policy=series_policy(), storage=storage),
    )

    r1 = await client.get("/series?id=A", expect=RESPONSE)
    assert r1 is not None
    assert base.count == 1
    assert r1.extensions.get("hishel_from_cache") is not True

    # The second identical GET is served from the cache, without reaching the network.
    r2 = await client.get("/series?id=A", expect=RESPONSE)
    assert r2 is not None
    assert base.count == 1
    assert r2.extensions.get("hishel_from_cache") is True


async def test_cache_miss_distinct_url_refetches(storage: AsyncSqliteStorage) -> None:
    base = _CountingTransport()
    client = StubClient(
        base_url="https://x",
        transport=base,
        rate=UNLIMITED,
        cache=HttpCache(policy=series_policy(), storage=storage),
    )

    await client.get("/series?id=A", expect=RESPONSE)
    await client.get("/series?id=B", expect=RESPONSE)
    # Distinct query params are distinct cache keys.
    assert base.count == 2


async def test_the_client_ttl_is_threaded_and_a_per_call_cache_ttl_wins(storage: AsyncSqliteStorage) -> None:
    """A client whose ``HttpCache`` has a ``ttl`` stamps it as ``hishel_ttl`` on every request.

    The ``FilterPolicy``, not the TTL, decides what is stored, and a per-call ``cache_ttl=`` wins over the client's.
    """
    base = _CountingTransport()
    client = StubClient(
        base_url="https://x",
        transport=base,
        rate=UNLIMITED,
        cache=HttpCache(policy=series_policy(), storage=storage, ttl=3600.0),
    )

    # Read off the network transport, so this checks what the wire sees however the client drives httpx2.
    captured = base.extensions

    await client.get("/series?id=A", expect=RESPONSE)
    assert captured[-1].get("hishel_ttl") == 3600.0

    # An unmatched path still carries the TTL: the FilterPolicy, not a missing TTL, leaves it uncached.
    await client.get("/other/path", expect=RESPONSE)
    assert captured[-1].get("hishel_ttl") == 3600.0

    # The per-call value is set first, and the transport's ``setdefault`` leaves it alone.
    await client.get("/series?id=C", expect=RESPONSE, cache_ttl=99.0)
    assert captured[-1].get("hishel_ttl") == 99.0


async def _network_hits_for_two_gets(
    storage: AsyncSqliteStorage, *, client_ttl: float | None, call_ttl: float | None, gap: float
) -> int:
    """Two GETs of one cacheable URL, *gap* seconds apart: how many of them reached the network.

    The clock hishel reads (``time.time``) is frozen and then SHIFTED by *gap*, so no test sleeps. The TTL is applied by
    hishel's storage, which compares an entry's ``created_at`` plus the ``hishel_ttl`` its stored request carried with
    that clock, so a TTL only hidden from hishel (but still delivered to the network transport) expires nothing.
    """
    base = _CountingTransport()
    client = StubClient(
        base_url="https://x",
        transport=base,
        rate=UNLIMITED,
        cache=HttpCache(policy=series_policy(), storage=storage, ttl=client_ttl),
    )
    with time_machine.travel(datetime.now(UTC), tick=False) as clock:
        await client.get("/series?id=A", expect=RESPONSE, cache_ttl=call_ttl)
        clock.shift(gap)
        await client.get("/series?id=A", expect=RESPONSE, cache_ttl=call_ttl)
    return base.count


async def test_a_short_client_ttl_expires_the_entry(storage: AsyncSqliteStorage) -> None:
    """hishel APPLIES the client's TTL: past it, the entry is gone and the second GET goes back to the network."""
    hits = await _network_hits_for_two_gets(storage, client_ttl=0.5, call_ttl=None, gap=2.0)

    assert hits == 2


async def test_a_long_client_ttl_keeps_the_entry(storage: AsyncSqliteStorage) -> None:
    """Control for the test above: the SAME gap inside a long TTL is a cache hit, so the expiry above is the TTL's."""
    hits = await _network_hits_for_two_gets(storage, client_ttl=3600.0, call_ttl=None, gap=2.0)

    assert hits == 1


async def test_a_per_call_cache_ttl_beats_the_client_ttl_inside_hishel(storage: AsyncSqliteStorage) -> None:
    """The per-call TTL wins where it matters, in the cache's expiry and not only on the request at the wire.

    It is on BOTH calls because hishel judges an entry by the TTL its STORED request carried: the first call's.
    """
    hits = await _network_hits_for_two_gets(storage, client_ttl=3600.0, call_ttl=0.5, gap=2.0)

    assert hits == 2


# --- A per-call cache_ttl= is forwarded by every verb that accepts one ---

#: Every public verb on the client that takes ``cache_ttl``, found rather than listed so a verb added later is covered.
_VERBS_TAKING_CACHE_TTL = sorted(
    name
    for name, member in vars(AsyncBaseClient).items()
    if not name.startswith("_")
    and inspect.iscoroutinefunction(member)
    and "cache_ttl" in inspect.signature(member).parameters
)


def test_the_verbs_taking_cache_ttl_are_the_six_http_methods() -> None:
    """The discovery above must actually find the verbs, or the parametrized test below is vacuous."""
    assert _VERBS_TAKING_CACHE_TTL == ["delete", "get", "head", "patch", "post", "put"]


@pytest.mark.parametrize("verb", _VERBS_TAKING_CACHE_TTL)
async def test_every_verb_forwards_a_per_call_cache_ttl(verb: str) -> None:
    """``cache_ttl=`` must reach the request the network sees (as ``hishel_ttl``) from EVERY verb, not just get/post."""
    base = _CountingTransport()
    client = StubClient(base_url="https://x", transport=base, rate=UNLIMITED)

    await getattr(client, verb)("/thing", expect=RESPONSE, cache_ttl=42.0)

    assert base.extensions[-1].get("hishel_ttl") == 42.0


# --- A client without a cache never caches, and a shared storage outlives its clients ---


async def test_never_cache_client_has_no_cache_transport() -> None:
    """A client built without a ``cache`` never caches: every repeated GET and POST reaches the network.

    The guarantee is structural (no ``cache``, no ``AsyncCacheTransport``), not a runtime check.
    """
    base = _CountingTransport()
    client = StubClient(rate=Rate.per_second(2), base_url="https://x", transport=base)

    assert not isinstance(_base_of(client), AsyncCacheTransport)

    for _ in range(3):
        await client.get("/series?id=A", expect=RESPONSE)
    assert base.count == 3
    for _ in range(2):
        await client.post("/series", expect=RESPONSE)
    assert base.count == 5


async def test_closing_clients_never_closes_their_shared_storage() -> None:
    """Closing every client that shares one storage leaves it open, for whoever created it to close.

    hishel's ``AsyncCacheTransport.aclose()`` closes its storage; ``ResilientCacheTransport.aclose()`` does not, or the
    first client's ``aclose()`` would close the storage under the rest.
    """
    conn = await anysqlite.connect(":memory:")
    shared = AsyncSqliteStorage(connection=conn)
    close_calls = {"n": 0}
    real_close = shared.close

    async def _counting_close() -> None:
        close_calls["n"] += 1
        await real_close()

    shared.close = _counting_close  # ty: ignore[invalid-assignment]  # an instance-level spy on the shared storage's close
    try:
        clients = [
            StubClient(
                rate=Rate.per_second(2),
                base_url="https://x",
                transport=_CountingTransport(),
                cache=HttpCache(policy=series_policy(), storage=shared),
            )
            for _ in range(3)
        ]
        for c in clients:
            ct = _base_of(c)
            assert isinstance(ct, AsyncCacheTransport)
            assert ct.storage is shared

        for c in clients:
            await c.aclose()
        assert close_calls["n"] == 0

        # Still usable after every client closed.
        entries = await shared.get_entries("nonexistent-key")
        assert entries == []
    finally:
        # Whoever created the storage is its only closer.
        await shared.close()
    assert close_calls["n"] == 1
