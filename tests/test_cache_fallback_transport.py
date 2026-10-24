""":class:`ResilientCacheTransport`: a storage blip must never fail a fetch.

The HTTP cache is an optimization, but a storage fault used to propagate and kill the whole request.

The contract pinned here, driven through the REAL hishel machinery (a genuinely faulting
:class:`hishel.AsyncBaseStorage`, not a stand-in transport):

  * a storage fault on LOOKUP (``get_entries``)  -> request served from the network
  * a storage fault on STORE  (``create_entry``) -> request still served (the write
    happens after the fetch, so this costs one extra fetch — safe, the cached surface
    is read-only)
  * a genuine ``httpx2.HTTPError``                -> re-raised untouched AND the
    network is hit exactly once, so the retry + breaker layers above keep their
    existing semantics
  * a healthy cache                              -> HIT short-circuits, no network
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, override

import anysqlite
import httpx2
import pytest
from hishel import (
    AsyncBaseStorage,
    AsyncSqliteStorage,
    Entry,
    FilterPolicy,
    Request,
    Response,
)

from headgate._cache import (
    MethodFilter,
    ResilientCacheTransport,
    StatusCodeFilter,
    _NoCloseStorage,
)

if TYPE_CHECKING:
    import uuid
    from collections.abc import AsyncIterator, Callable

_URL = "https://example.test/series?api_key=secret"

# Sentinel for "this storage never faults" (a method name nothing calls).
_NEVER = "never"


@pytest.fixture
async def storage() -> AsyncIterator[AsyncSqliteStorage]:
    """A REAL hishel storage (in-memory sqlite) — same idiom as test_cache_wiring."""
    conn = await anysqlite.connect(":memory:")
    store = AsyncSqliteStorage(connection=conn)
    try:
        yield store
    finally:
        await store.close()


class StorageTimeout(Exception):
    """Stands in for a backend timeout raised from inside the storage: not an httpx2 error."""


class StorageUnreachable(Exception):
    """Stands in for a backend connection failure raised from inside the storage."""


class _Network(httpx2.AsyncBaseTransport):
    """The real network leg — counts fetches."""

    def __init__(self, exc: Exception | None = None) -> None:
        self.calls = 0
        self._exc = exc

    @override
    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        self.calls += 1
        if self._exc is not None:
            raise self._exc
        return httpx2.Response(200, text="from-network")


class _Faulty(AsyncBaseStorage):
    """Delegate to a real storage, but raise *exc* from ONE named method.

    Mimics a storage blip on exactly one leg (lookup or store) while every other
    operation behaves normally — so the healthy paths stay genuinely healthy.
    """

    def __init__(self, inner: AsyncBaseStorage, exc: Exception, *, failing: str) -> None:
        self._inner = inner
        self._exc = exc
        self._failing = failing

    def _maybe_fail(self, method: str) -> None:
        if method == self._failing:
            raise self._exc

    @override
    async def create_entry(
        self,
        request: Request,
        response: Response,
        key: str,
        id_: uuid.UUID | None = None,
    ) -> Entry:
        self._maybe_fail("create_entry")
        return await self._inner.create_entry(request, response, key, id_)

    @override
    async def get_entries(self, key: str) -> list[Entry]:
        self._maybe_fail("get_entries")
        return await self._inner.get_entries(key)

    @override
    async def update_entry(
        self,
        id: uuid.UUID,
        new_entry: Entry | Callable[[Entry], Entry],
    ) -> Entry | None:
        return await self._inner.update_entry(id, new_entry)

    @override
    async def remove_entry(self, id: uuid.UUID) -> None:
        await self._inner.remove_entry(id)

    @override
    async def refresh_entry_ttl(self, id: uuid.UUID) -> None:
        await self._inner.refresh_entry_ttl(id)

    @override
    def is_soft_deleted(self, pair: Entry) -> bool:
        return self._inner.is_soft_deleted(pair)

    @override
    def is_safe_to_hard_delete(self, pair: Entry) -> bool:
        return self._inner.is_safe_to_hard_delete(pair)

    @override
    def mark_pair_as_deleted(self, pair: Entry) -> Entry:
        return self._inner.mark_pair_as_deleted(pair)

    @override
    async def close(self) -> None:
        await self._inner.close()


def _policy() -> FilterPolicy:
    """Cache every GET — so hishel genuinely reaches the storage on each request."""
    return FilterPolicy(
        request_filters=[MethodFilter(frozenset({"GET"}))],
        response_filters=[StatusCodeFilter(frozenset({200}))],
    )


def _transport(inner: AsyncBaseStorage, network: httpx2.AsyncBaseTransport) -> ResilientCacheTransport:
    return ResilientCacheTransport(
        next_transport=network,
        storage=_NoCloseStorage(inner),
        policy=_policy(),
        max_body_bytes=1024 * 1024,
    )


@pytest.mark.parametrize(
    "exc",
    [
        StorageTimeout("timeout reading from storage"),  # the live failure
        StorageUnreachable("connection refused"),
        ValueError("corrupt cache entry"),  # any non-httpx storage fault
    ],
)
async def test_lookup_fault_degrades_to_network(exc: Exception, storage: AsyncSqliteStorage) -> None:
    network = _Network()
    faulty = _Faulty(storage, exc, failing="get_entries")

    resp = await _transport(faulty, network).handle_async_request(httpx2.Request("GET", _URL))

    assert resp.status_code == 200
    assert resp.text == "from-network"
    assert network.calls == 1  # served uncached rather than failing the fetch


async def test_store_fault_still_serves_the_request(storage: AsyncSqliteStorage) -> None:
    # A regression seen in production: create_entry blew up AFTER a healthy fetch.
    network = _Network()
    faulty = _Faulty(
        storage,
        StorageTimeout("timeout writing to storage"),
        failing="create_entry",
    )

    resp = await _transport(faulty, network).handle_async_request(httpx2.Request("GET", _URL))

    assert resp.status_code == 200
    # A store fault surfaces only after the fetch, so the degraded path refetches.
    # That is the accepted cost: the cached surface is read-only, so a replay is safe.
    assert network.calls == 2


@pytest.mark.parametrize(
    "exc",
    [httpx2.ConnectTimeout("network down"), httpx2.ConnectError("dns")],
)
async def test_genuine_httpx_error_is_reraised_and_not_refetched(exc: Exception, storage: AsyncSqliteStorage) -> None:
    # A real transport failure is NOT the cache's fault: it must surface unchanged so
    # retry/breaker behave exactly as before, and must NOT trigger a second fetch.
    network = _Network(exc=exc)
    faulty = _Faulty(storage, RuntimeError("unused"), failing=_NEVER)

    with pytest.raises(type(exc)):
        await _transport(faulty, network).handle_async_request(httpx2.Request("GET", _URL))

    assert network.calls == 1


async def test_healthy_cache_hit_never_touches_the_network(
    storage: AsyncSqliteStorage,
) -> None:
    # Subclassing must not disturb hishel's own hit path: fetch once, then serve the
    # second identical request from storage.
    network = _Network()
    faulty = _Faulty(storage, RuntimeError("unused"), failing=_NEVER)
    transport = _transport(faulty, network)

    first = await transport.handle_async_request(httpx2.Request("GET", _URL))
    await first.aread()
    second = await transport.handle_async_request(httpx2.Request("GET", _URL))
    await second.aread()

    assert first.status_code == second.status_code == 200
    assert network.calls == 1  # the second request was a HIT


class _TrackedStream(httpx2.AsyncByteStream):
    """A network body that records whether anything closed it."""

    def __init__(self) -> None:
        self.closed = False

    @override
    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield b"from-network"

    @override
    async def aclose(self) -> None:
        self.closed = True


class _TrackingNetwork(httpx2.AsyncBaseTransport):
    """The network leg, keeping every body it hands out and every URL it was asked for."""

    def __init__(self) -> None:
        self.streams: list[_TrackedStream] = []
        self.urls: list[str] = []

    @override
    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        self.urls.append(str(request.url))
        stream = _TrackedStream()
        self.streams.append(stream)
        return httpx2.Response(200, stream=stream)


async def test_a_store_fault_closes_the_network_response_it_abandoned(storage: AsyncSqliteStorage) -> None:
    """hishel sends first and stores second; when the store raised, its unread network response held a pooled
    connection forever. After ``max_connections`` storage faults every cache MISS waited out the pool timeout."""
    network = _TrackingNetwork()
    faulty = _Faulty(storage, StorageTimeout("timeout writing to storage"), failing="create_entry")

    resp = await _transport(faulty, network).handle_async_request(httpx2.Request("GET", _URL))
    await resp.aread()

    assert len(network.streams) == 2, "the fault fell back to one uncached fetch"
    assert network.streams[0].closed, "the response hishel abandoned must be closed, releasing its connection"


class _BodyFaulty(_Faulty):
    """Store normally, but hand back an entry whose BODY faults part-way — a backend write per chunk on a MISS."""

    @override
    async def create_entry(
        self,
        request: Request,
        response: Response,
        key: str,
        id_: uuid.UUID | None = None,
    ) -> Entry:
        entry = await self._inner.create_entry(request, response, key, id_)
        exc = self._exc

        async def _dies() -> AsyncIterator[bytes]:
            yield b"from-"
            raise exc

        return dataclasses.replace(entry, response=dataclasses.replace(entry.response, stream=_dies()))


async def test_a_fault_while_the_body_streams_degrades_to_network(storage: AsyncSqliteStorage) -> None:
    """The body used to be read after this transport returned, so a storage fault there escaped as a raw
    a raw backend exception — not a ``ClientError`` — and failed a fetch that would have succeeded."""
    network = _Network()
    faulty = _BodyFaulty(storage, StorageTimeout("timeout reading from storage"), failing=_NEVER)

    resp = await _transport(faulty, network).handle_async_request(httpx2.Request("GET", _URL))
    body = await resp.aread()

    assert body == b"from-network"
    assert "headgate_cache_degraded" in resp.extensions


async def test_credential_query_params_never_reach_storage(storage: AsyncSqliteStorage) -> None:
    """hishel persists the full request URL with every entry, so a key on the query string was stored in plaintext.
    Stripped before hishel and restored below it: storage never sees it, the network does, and a hit still matches."""
    stored_urls: list[str] = []

    class _Recording(_Faulty):
        @override
        async def create_entry(
            self,
            request: Request,
            response: Response,
            key: str,
            id_: uuid.UUID | None = None,
        ) -> Entry:
            stored_urls.append(str(request.url))
            return await self._inner.create_entry(request, response, key, id_)

    network = _TrackingNetwork()
    transport = _transport(_Recording(storage, RuntimeError("unused"), failing=_NEVER), network)

    request = httpx2.Request("GET", _URL)
    first = await transport.handle_async_request(request)
    await first.aread()
    second = await transport.handle_async_request(httpx2.Request("GET", _URL))
    await second.aread()

    assert stored_urls, "the response was stored"
    assert all("secret" not in url for url in stored_urls), stored_urls
    assert len(network.urls) == 1, "the second request was a HIT"
    assert "api_key=secret" in network.urls[0], "the network request still carried the key"
    assert "api_key=secret" in str(request.url), "the caller's request keeps its key, so a retry re-sends it"
