"""AsyncBaseClient cache transport-stack assembly + HIT/MISS behaviour.

The cache transport is opt-in per client (``cache=HttpCache(policy=, storage=, ttl=)``) and
below retry and above the rate limiter: ``RetryTransport(AsyncCacheTransport(
RateLimitTransport(base)))``. A client that passes no
``cache`` (or an ``HttpCache`` with no ``storage``) builds a byte-identical stack to today — NO
``AsyncCacheTransport``. Caching only engages when the ``HttpCache`` carries a
``storage`` (its ``policy`` is required); otherwise it degrades to the plain stack. The
``HttpCache``'s ``ttl`` is threaded onto every request as the ``hishel_ttl`` extension by the cache transport (the
``FilterPolicy`` — not the TTL — decides *what* to store); a per-call
``cache_ttl=`` wins (the transport's ``setdefault``).
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, override
from unittest.mock import MagicMock

import anysqlite
import httpx2
import pytest
import time_machine
from hishel import AsyncSqliteStorage, FilterPolicy
from hishel.httpx2 import AsyncCacheTransport

from headgate._cache import (
    HttpCache,
    NoCloseStorage,
    PathFragmentFilter,
    StatusCodeFilter,
    _RestoreRequestExtensions,
)
from headgate._client import AsyncBaseClient
from headgate._rate_limit import UNLIMITED, Rate, RateLimitTransport
from headgate._retry import RetryTransport
from tests.support.responses import RESPONSE

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


class _StubClient(AsyncBaseClient):
    """Minimal AsyncBaseClient subclass for cache-stack tests."""


def _series_policy() -> FilterPolicy:
    """A tiny native FilterPolicy caching only ``/series`` paths (200-only)."""
    return FilterPolicy(
        request_filters=[PathFragmentFilter(("/series",))],
        response_filters=[StatusCodeFilter(frozenset({200}))],
    )


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
    # A client without a ``cache`` is byte-identical to today:
    # RetryTransport(RateLimitTransport(base)) — NO AsyncCacheTransport.
    base = httpx2.MockTransport(lambda r: httpx2.Response(200))
    client = _StubClient(rate=Rate.per_second(2), base_url="https://x", transport=base)

    below_retry = _base_of(client)
    assert not isinstance(below_retry, AsyncCacheTransport)
    assert _network_of(client) is base


def test_cache_without_storage_builds_plain_stack() -> None:
    # an HttpCache whose storage is None ⇒ degrade to plain stack.
    base = httpx2.MockTransport(lambda r: httpx2.Response(200))
    client = _StubClient(
        base_url="https://x", transport=base, rate=UNLIMITED, cache=HttpCache(policy=_series_policy(), storage=None)
    )

    below_retry = _base_of(client)
    assert not isinstance(below_retry, AsyncCacheTransport)
    assert _network_of(client) is base


def test_cache_with_storage_inserts_cache_transport_base_adjacent() -> None:
    # an HttpCache with a storage ⇒ AsyncCacheTransport sits base-adjacent:
    # RetryTransport(AsyncCacheTransport(RateLimitTransport(base))).
    storage = MagicMock(name="storage")
    base = httpx2.MockTransport(lambda r: httpx2.Response(200))
    client = _StubClient(
        base_url="https://x",
        transport=base,
        rate=UNLIMITED,
        cache=HttpCache(policy=_series_policy(), storage=storage),
    )

    cache_transport = _base_of(client)
    assert isinstance(cache_transport, AsyncCacheTransport)
    # base-adjacent: below the cache sits only the rate limiter, then the network.
    assert _network_of(client) is base
    # The storage handed to the transport is a NoCloseStorage wrapper (so a
    # per-adapter teardown can't close the shared client).
    assert isinstance(cache_transport.storage, NoCloseStorage)
    assert cache_transport.storage._inner is storage


# ---------------------------------------------------------------------------
# HIT / MISS behaviour + per-request hishel_ttl threading (real SQLite storage).
# ---------------------------------------------------------------------------


class _CountingTransport(httpx2.AsyncBaseTransport):
    """MockTransport wrapper that counts how many requests reach the network."""

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


@pytest.fixture
async def sqlite_storage() -> AsyncIterator[AsyncSqliteStorage]:
    """An in-memory hishel SQLite storage (no remote needed for CI)."""
    conn = await anysqlite.connect(":memory:")
    storage = AsyncSqliteStorage(connection=conn)
    try:
        yield storage
    finally:
        await storage.close()


async def test_cache_hit_second_call_no_network(
    sqlite_storage: AsyncSqliteStorage,
) -> None:
    base = _CountingTransport()
    client = _StubClient(
        base_url="https://x",
        transport=base,
        rate=UNLIMITED,
        cache=HttpCache(policy=_series_policy(), storage=sqlite_storage),
    )

    r1 = await client.get("/series?id=A", expect=RESPONSE)
    assert r1 is not None
    assert base.count == 1
    assert r1.extensions.get("hishel_from_cache") is not True

    # Second identical GET is served from cache — the mock is NOT hit again.
    r2 = await client.get("/series?id=A", expect=RESPONSE)
    assert r2 is not None
    assert base.count == 1
    assert r2.extensions.get("hishel_from_cache") is True


async def test_cache_miss_distinct_url_refetches(
    sqlite_storage: AsyncSqliteStorage,
) -> None:
    base = _CountingTransport()
    client = _StubClient(
        base_url="https://x",
        transport=base,
        rate=UNLIMITED,
        cache=HttpCache(policy=_series_policy(), storage=sqlite_storage),
    )

    await client.get("/series?id=A", expect=RESPONSE)
    await client.get("/series?id=B", expect=RESPONSE)
    # Distinct query params ⇒ distinct cache keys ⇒ 2 network calls.
    assert base.count == 2


async def test_the_client_ttl_is_threaded_and_a_per_call_cache_ttl_wins(
    sqlite_storage: AsyncSqliteStorage,
) -> None:
    """A client whose ``HttpCache`` has a ``ttl`` threads it as ``hishel_ttl`` on EVERY request (the FilterPolicy —
    not the TTL — decides storage), and a per-call ``cache_ttl=`` wins over it."""
    base = _CountingTransport()
    client = _StubClient(
        base_url="https://x",
        transport=base,
        rate=UNLIMITED,
        cache=HttpCache(policy=_series_policy(), storage=sqlite_storage, ttl=3600.0),
    )

    # the cache transport stamps the client's ttl into the request's extensions before it goes out,
    # and what the TRANSPORT receives is exactly what this asserts on — read
    # straight off the recording transport rather than spying on an httpx2
    # internal, so it stays valid however the client drives httpx2.
    captured = base.extensions

    # Constant TTL threaded on a matched path.
    await client.get("/series?id=A", expect=RESPONSE)
    assert captured[-1].get("hishel_ttl") == 3600.0

    # A non-matched path STILL carries the constant TTL — it's the FilterPolicy
    # that passes the request through uncached, not the absence of a TTL.
    await client.get("/other/path", expect=RESPONSE)
    assert captured[-1].get("hishel_ttl") == 3600.0

    # A per-call ``cache_ttl=`` is an assignment, and the transport's ``setdefault`` leaves it alone.
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
    client = _StubClient(
        base_url="https://x",
        transport=base,
        rate=UNLIMITED,
        cache=HttpCache(policy=_series_policy(), storage=storage, ttl=client_ttl),
    )
    with time_machine.travel(datetime.now(UTC), tick=False) as clock:
        await client.get("/series?id=A", expect=RESPONSE, cache_ttl=call_ttl)
        clock.shift(gap)
        await client.get("/series?id=A", expect=RESPONSE, cache_ttl=call_ttl)
    return base.count


async def test_a_short_client_ttl_expires_the_entry(sqlite_storage: AsyncSqliteStorage) -> None:
    """hishel APPLIES the client's TTL: past it, the entry is gone and the second GET goes back to the network."""
    hits = await _network_hits_for_two_gets(sqlite_storage, client_ttl=0.5, call_ttl=None, gap=2.0)

    assert hits == 2


async def test_a_long_client_ttl_keeps_the_entry(sqlite_storage: AsyncSqliteStorage) -> None:
    """Control for the test above: the SAME gap inside a long TTL is a cache hit, so the expiry above is the TTL's."""
    hits = await _network_hits_for_two_gets(sqlite_storage, client_ttl=3600.0, call_ttl=None, gap=2.0)

    assert hits == 1


async def test_a_per_call_cache_ttl_beats_the_client_ttl_inside_hishel(sqlite_storage: AsyncSqliteStorage) -> None:
    """The per-call TTL wins where it matters, in the cache's expiry and not only on the request at the wire.

    It is on BOTH calls because hishel judges an entry by the TTL its STORED request carried: the first call's.
    """
    hits = await _network_hits_for_two_gets(sqlite_storage, client_ttl=3600.0, call_ttl=0.5, gap=2.0)

    assert hits == 2


# ---------------------------------------------------------------------------
# A per-call cache_ttl= is forwarded by every verb that accepts one.
# ---------------------------------------------------------------------------

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
    client = _StubClient(base_url="https://x", transport=base, rate=UNLIMITED)

    await getattr(client, verb)("/thing", expect=RESPONSE, cache_ttl=42.0)

    assert base.extensions[-1].get("hishel_ttl") == 42.0


# ---------------------------------------------------------------------------
# Structural never-cache regression lock (HARD never-cache invariant).
# ---------------------------------------------------------------------------


async def test_never_cache_client_has_no_cache_transport() -> None:
    """A client built WITHOUT a ``cache`` (a live quotes/bars/orders/account
    client) NEVER caches. Repeated identical
    GET/POST always hit the network (call count == N). The never-cache guarantee
    is structural (no cache ⇒ no AsyncCacheTransport), not a runtime
    check."""
    base = _CountingTransport()
    # NO cache — this simulates a trading / live-quote client that never opts
    # into caching, and so is never handed the boot storage.
    client = _StubClient(rate=Rate.per_second(2), base_url="https://x", transport=base)

    # No cache transport exists.
    assert not isinstance(_base_of(client), AsyncCacheTransport)

    # Repeated identical requests always reach the network.
    for _ in range(3):
        await client.get("/series?id=A", expect=RESPONSE)
    assert base.count == 3
    # A POST to a stable URL likewise never caches.
    for _ in range(2):
        await client.post("/series", expect=RESPONSE)
    assert base.count == 5


async def test_multi_adapter_teardown_survives_shared_storage() -> None:
    """N AsyncBaseClient adapters sharing ONE storage tear down; the shared
    storage survives until the composition root closes it (the NoCloseStorage
    wrapper is load-bearing — hishel's AsyncCacheTransport.aclose() closes its
    storage, so without the wrapper the FIRST adapter teardown would close the
    shared client and break the rest)."""
    # A real inner storage so close() is observable.
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
            _StubClient(
                rate=Rate.per_second(2),
                base_url="https://x",
                transport=_CountingTransport(),
                cache=HttpCache(policy=_series_policy(), storage=shared),
            )
            for _ in range(3)
        ]
        # Each adapter got its own cache transport over the SAME shared storage,
        # each wrapped so its close() is a no-op.
        for c in clients:
            ct = _base_of(c)
            assert isinstance(ct, AsyncCacheTransport)
            assert isinstance(ct.storage, NoCloseStorage)
            assert ct.storage._inner is shared

        # Tearing every adapter down must NOT close the shared storage.
        for c in clients:
            await c.aclose()
        assert close_calls["n"] == 0

        # The shared storage is still usable after all adapters unwound.
        entries = await shared.get_entries("nonexistent-key")
        assert entries == []
    finally:
        # The composition root is the ONLY closer of the real storage.
        await shared.close()
    assert close_calls["n"] == 1
