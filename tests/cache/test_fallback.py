# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

""":class:`ResilientCacheTransport`: a storage fault never fails a fetch, and nothing else is mistaken for one.

The HTTP cache is an optimization, so a fault inside the storage degrades to an uncached fetch. Driven through the real
hishel machinery, with a genuinely faulting :class:`hishel.AsyncBaseStorage` rather than a stand-in transport:

* a fault on lookup (``get_entries``) is served from the network;
* a fault on store (``create_entry``) is still served, at the cost of one extra fetch (the store happens after the
  fetch; the cached surface is read-only, so the replay is safe);
* a fault while a stored body streams is served from the network;
* a genuine ``httpx2.HTTPError`` is re-raised untouched after exactly one fetch, even when it surfaces from inside the
  storage, so the retry and breaker layers above see it as they would without a cache;
* anything else raised inside hishel is a bug, not an outage, and propagates;
* a healthy cache hit short-circuits, with no network call.
"""

from __future__ import annotations

import asyncio
import dataclasses
import sqlite3
from typing import TYPE_CHECKING, override

import httpx2
import pytest
from hishel import AsyncBaseStorage, AsyncSqliteStorage, BaseFilter, Entry, FilterPolicy, Request, Response

from headgate import HttpCache, HttpOptions, TransportError
from headgate._breaker import HOST_BREAKERS
from headgate._cache import CACHE_DEGRADATIONS, CACHE_DEGRADED_EXT, HOST_UNTESTED, CacheStorageError, host_untested
from tests.cache._helpers import (
    NETWORK_BODY,
    FaultyStorage,
    Network,
    TrackedStream,
    TrackingNetwork,
    cache_transport,
    every_get_policy,
    stub_client,
)

if TYPE_CHECKING:
    import uuid
    from collections.abc import AsyncIterable, AsyncIterator, Awaitable, Callable, Iterator

_HOST = "example.test"
_URL = f"https://{_HOST}/series?api_key=secret"


class StorageTimeoutError(Exception):
    """Stands in for a backend timeout raised from inside the storage: not an httpx2 error."""


class StorageUnreachableError(Exception):
    """Stands in for a backend connection failure raised from inside the storage."""


@pytest.mark.parametrize(
    "exc",
    [
        StorageTimeoutError("timeout reading from storage"),
        StorageUnreachableError("connection refused"),
        ValueError("corrupt cache entry"),
    ],
)
async def test_lookup_fault_degrades_to_network(exc: Exception, storage: AsyncSqliteStorage) -> None:
    """A lookup that raises any non-httpx2 error is served from the network, once, and marked degraded."""
    network = Network()
    faulty = FaultyStorage(storage, exc, failing="get_entries")

    resp = await cache_transport(faulty, network).handle_async_request(httpx2.Request("GET", _URL))

    assert resp.status_code == httpx2.codes.OK
    assert await resp.aread() == NETWORK_BODY
    assert network.calls == 1
    assert type(exc).__name__ in resp.extensions[CACHE_DEGRADED_EXT]
    assert "secret" not in resp.extensions[CACHE_DEGRADED_EXT], "the query, and any key in it, is dropped"


async def test_the_degraded_marker_names_the_backend_error_by_type_alone(storage: AsyncSqliteStorage) -> None:
    """A backend's message can carry its DSN and password, so the marker a caller can read never repeats it."""
    faulty = FaultyStorage(
        storage, sqlite3.OperationalError("unable to open postgres://admin:hunter2@db"), failing="get_entries"
    )

    resp = await cache_transport(faulty, Network()).handle_async_request(httpx2.Request("GET", _URL))

    assert resp.extensions[CACHE_DEGRADED_EXT] == f"https://{_HOST}/series: OperationalError"


async def test_store_fault_still_serves_the_request(storage: AsyncSqliteStorage) -> None:
    """``create_entry`` raises after a healthy fetch, and the request is redone live."""
    network = Network()
    faulty = FaultyStorage(storage, StorageTimeoutError("timeout writing to storage"), failing="create_entry")

    resp = await cache_transport(faulty, network).handle_async_request(httpx2.Request("GET", _URL))

    assert resp.status_code == httpx2.codes.OK
    assert network.calls == len(("the fetch hishel made", "the live refetch"))


@pytest.mark.parametrize("exc", [httpx2.ConnectTimeout("network down"), httpx2.ConnectError("dns")])
async def test_genuine_httpx_error_is_reraised_and_not_refetched(exc: Exception, storage: AsyncSqliteStorage) -> None:
    """A transport failure is not the cache's fault: it surfaces unchanged, after one fetch."""
    network = Network(exc=exc)

    with pytest.raises(type(exc)):
        await cache_transport(storage, network).handle_async_request(httpx2.Request("GET", _URL))

    assert network.calls == 1


async def test_a_network_failure_while_the_storage_stores_the_body_is_not_a_storage_fault(
    storage: AsyncSqliteStorage,
) -> None:
    """A storage writes a miss's body as it streams it from the network, so a network error surfaces inside it.

    That error is still the network's: it reaches the retry layer as itself, after one fetch, with nothing degraded.
    """
    network = _FlakyNetwork()
    degraded_before = CACHE_DEGRADATIONS[_HOST]

    with pytest.raises(httpx2.ReadError):
        await cache_transport(storage, network).handle_async_request(httpx2.Request("GET", _URL))

    assert len(network.urls) == 1
    assert network.streams[0].closed, "the network response is closed"
    assert CACHE_DEGRADATIONS[_HOST] == degraded_before


class _DropsMidBody(TrackedStream):
    """A network body that sends one chunk and then fails with *exc*: by default, a lost connection."""

    def __init__(self, exc: Exception | None = None) -> None:
        """Fail with *exc*, or an ``httpx2.ReadError`` when none is given."""
        super().__init__()
        self._exc = exc or httpx2.ReadError("connection reset")

    @override
    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield b"from-"
        raise self._exc


class _FlakyNetwork(TrackingNetwork):
    """The network leg, answering every request with a body that fails part-way with *exc*."""

    def __init__(self, exc: Exception | None = None) -> None:
        """Fail every body with *exc*, or an ``httpx2.ReadError`` when none is given."""
        super().__init__()
        self._exc = exc

    @override
    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        self.urls.append(str(request.url))
        stream = _DropsMidBody(self._exc)
        self.streams.append(stream)
        return httpx2.Response(httpx2.codes.OK, stream=stream)


async def test_an_oserror_while_the_storage_stores_the_body_is_a_network_failure(storage: AsyncSqliteStorage) -> None:
    """A custom transport's ``OSError`` mid-body is a network failure, as the retry layer counts it, not a fault.

    It reaches the retry layer as itself after one fetch, rather than being redone live and counted as a degradation.
    """
    network = _FlakyNetwork(ConnectionResetError("peer reset"))
    degraded_before = CACHE_DEGRADATIONS[_HOST]

    with pytest.raises(ConnectionResetError):
        await cache_transport(storage, network).handle_async_request(httpx2.Request("GET", _URL))

    assert len(network.urls) == 1
    assert CACHE_DEGRADATIONS[_HOST] == degraded_before


async def test_an_oserror_from_the_storage_itself_is_still_a_storage_fault(storage: AsyncSqliteStorage) -> None:
    """Outside the body of a miss an ``OSError`` comes from the backend (a full disk), so the request degrades."""
    network = Network()
    faulty = FaultyStorage(storage, OSError("No space left on device"), failing="get_entries")

    resp = await cache_transport(faulty, network).handle_async_request(httpx2.Request("GET", _URL))

    assert await resp.aread() == NETWORK_BODY
    assert resp.extensions[CACHE_DEGRADED_EXT].endswith(": OSError")


async def test_a_network_failure_in_the_degraded_fetch_is_raised_and_closes_it(storage: AsyncSqliteStorage) -> None:
    """The live redo's body is read inside the layer too: a network failure there surfaces as itself, closed."""
    network = _FlakyNetwork()
    faulty = FaultyStorage(storage, StorageTimeoutError("timeout"), failing="get_entries")

    with pytest.raises(httpx2.ReadError):
        await cache_transport(faulty, network).handle_async_request(httpx2.Request("GET", _URL))

    assert len(network.urls) == 1
    assert network.streams[0].closed


async def test_a_bug_inside_hishel_propagates_rather_than_degrading(storage: AsyncSqliteStorage) -> None:
    """Only a storage fault falls back: an error from a filter is a bug to see, not an outage to hide."""

    class _BrokenFilter(BaseFilter[Request]):
        @override
        def needs_body(self) -> bool:
            return False

        @override
        def apply(self, item: Request, body: bytes | None) -> bool:
            msg = "a bug in a filter"
            raise RuntimeError(msg)

    network = Network()
    degraded_before = CACHE_DEGRADATIONS[_HOST]
    transport = cache_transport(storage, network, policy=FilterPolicy(request_filters=[_BrokenFilter()]))

    with pytest.raises(RuntimeError, match="a bug in a filter"):
        await transport.handle_async_request(httpx2.Request("GET", _URL))

    assert network.calls == 0
    assert CACHE_DEGRADATIONS[_HOST] == degraded_before


async def test_healthy_cache_hit_never_touches_the_network(storage: AsyncSqliteStorage) -> None:
    """The subclass leaves hishel's hit path intact: one fetch, then the identical request is served from storage."""
    network = Network()
    transport = cache_transport(FaultyStorage(storage), network)

    first = await transport.handle_async_request(httpx2.Request("GET", _URL))
    await first.aread()
    second = await transport.handle_async_request(httpx2.Request("GET", _URL))
    await second.aread()

    assert first.status_code == second.status_code == httpx2.codes.OK
    assert network.calls == 1, "the second request was a hit"


async def test_a_store_fault_closes_the_network_response_it_abandoned(storage: AsyncSqliteStorage) -> None:
    """Hishel sends first and stores second, so a store fault abandons an unread network response.

    That response is closed before the fallback fetch; left open it would hold its pooled connection, and after
    ``max_connections`` store faults every cache miss would wait out the pool timeout.
    """
    network = TrackingNetwork()
    faulty = FaultyStorage(storage, StorageTimeoutError("timeout writing to storage"), failing="create_entry")

    resp = await cache_transport(faulty, network).handle_async_request(httpx2.Request("GET", _URL))
    await resp.aread()

    assert len(network.streams) == len(("the fetch hishel made", "the live refetch"))
    assert network.streams[0].closed, "the response hishel abandoned is closed, releasing its connection"


class _BodyFaulty(FaultyStorage):
    """Store normally, but hand back an entry whose body faults part-way, as a backend writing per chunk can."""

    @override
    async def create_entry(self, request: Request, response: Response, key: str, id_: uuid.UUID | None = None) -> Entry:
        entry = await self.inner.create_entry(request, response, key, id_)
        exc = self.exc

        async def _dies() -> AsyncIterator[bytes]:
            yield b"from-"
            if exc is not None:
                raise exc

        return dataclasses.replace(entry, response=dataclasses.replace(entry.response, stream=_dies()))


async def test_a_fault_while_the_body_streams_degrades_to_network(storage: AsyncSqliteStorage) -> None:
    """The body is read inside this transport, so a storage fault while it streams degrades to the network.

    Read later, the fault would escape as a raw backend exception, not a ``ClientError``, and fail a fetch that would
    have succeeded.
    """
    network = Network()
    faulty = _BodyFaulty(storage, StorageTimeoutError("timeout reading from storage"))

    resp = await cache_transport(faulty, network).handle_async_request(httpx2.Request("GET", _URL))

    assert await resp.aread() == NETWORK_BODY
    assert CACHE_DEGRADED_EXT in resp.extensions


class _SyncBody(FaultyStorage):
    """Return stored entries whose body is a synchronous iterator, which an async storage must never hand back."""

    @override
    async def get_entries(self, key: str) -> list[Entry]:
        def _sync() -> Iterator[bytes]:
            yield b"stored"

        entries = await self.inner.get_entries(key)
        return [dataclasses.replace(e, response=dataclasses.replace(e.response, stream=_sync())) for e in entries]


async def test_a_storage_that_returns_a_synchronous_body_degrades_to_network(storage: AsyncSqliteStorage) -> None:
    """A misbehaving storage is a storage fault like any other: the hit it cannot serve is redone live."""
    network = Network()
    transport = cache_transport(_SyncBody(storage), network)

    first = await transport.handle_async_request(httpx2.Request("GET", _URL))
    await first.aread()
    second = await transport.handle_async_request(httpx2.Request("GET", _URL))

    assert await second.aread() == NETWORK_BODY
    assert second.extensions[CACHE_DEGRADED_EXT].endswith(": TypeError"), "the backend error is named by type alone"
    assert network.calls == len(("the first fetch", "the live redo of the broken hit"))


async def test_a_degraded_response_over_the_cap_is_still_served_whole(storage: AsyncSqliteStorage) -> None:
    """A live response past the cap is handed back as it is, for the client's own size check to refuse."""
    network = Network()
    faulty = FaultyStorage(storage, StorageTimeoutError("timeout"), failing="get_entries")
    transport = cache_transport(faulty, network, cap=len(NETWORK_BODY) - 1)

    resp = await transport.handle_async_request(httpx2.Request("GET", _URL))

    assert await resp.aread() == NETWORK_BODY
    assert CACHE_DEGRADED_EXT in resp.extensions


class _StallsAfterFirstChunk(TrackedStream):
    """A network body that sends one chunk, sets *stalled*, and then never sends another."""

    def __init__(self, stalled: asyncio.Event) -> None:
        super().__init__()
        self._stalled = stalled

    @override
    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield b"from-"
        self._stalled.set()
        await asyncio.Event().wait()


async def test_a_cancelled_fetch_closes_the_network_response(storage: AsyncSqliteStorage) -> None:
    """Cancelled while the body is read inside the layer, the fetch closes what it abandons and leaks no connection."""
    stalled = asyncio.Event()

    class _Stalling(TrackingNetwork):
        @override
        async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
            stream = _StallsAfterFirstChunk(stalled)
            self.streams.append(stream)
            return httpx2.Response(httpx2.codes.OK, stream=stream)

    network = _Stalling()
    task = asyncio.create_task(cache_transport(storage, network).handle_async_request(httpx2.Request("GET", _URL)))
    await stalled.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert network.streams[0].closed


async def test_credential_query_params_never_reach_storage(storage: AsyncSqliteStorage) -> None:
    """Hishel persists the full request URL with every entry, so a key on the query string would be stored in plaintext.

    The key is stripped before hishel and restored below it: storage never sees it, the network does, and a hit still
    matches.
    """
    stored_urls: list[str] = []

    class _Recording(FaultyStorage):
        @override
        async def create_entry(
            self, request: Request, response: Response, key: str, id_: uuid.UUID | None = None
        ) -> Entry:
            stored_urls.append(str(request.url))
            return await self.inner.create_entry(request, response, key, id_)

    network = TrackingNetwork()
    transport = cache_transport(_Recording(storage), network)

    request = httpx2.Request("GET", _URL)
    first = await transport.handle_async_request(request)
    await first.aread()
    second = await transport.handle_async_request(httpx2.Request("GET", _URL))
    await second.aread()

    assert stored_urls, "the response was stored"
    assert all("secret" not in url for url in stored_urls), stored_urls
    assert len(network.urls) == 1, "the second request was a hit"
    assert "api_key=secret" in network.urls[0], "the network request still carried the key"
    assert "api_key=secret" in str(request.url), "the caller's request keeps its key, so a retry re-sends it"


async def test_the_network_gets_the_url_exactly_as_sent(storage: AsyncSqliteStorage) -> None:
    """A repeated credential param keeps every value, and every param its place, on the way to the network."""
    sent = f"https://{_HOST}/series?api_key=first&id=1&api_key=second"
    network = TrackingNetwork()

    response = await cache_transport(storage, network).handle_async_request(httpx2.Request("GET", sent))
    await response.aread()

    assert network.urls == [sent]


#: Each guarded storage operation the transport's tests do not otherwise reach, called on an entry id.
_OPERATIONS: dict[str, Callable[[AsyncBaseStorage, uuid.UUID], Awaitable[object]]] = {
    "update_entry": lambda store, entry_id: store.update_entry(entry_id, lambda entry: entry),
    "remove_entry": lambda store, entry_id: store.remove_entry(entry_id),
    "refresh_entry_ttl": lambda store, entry_id: store.refresh_entry_ttl(entry_id),
}


@pytest.mark.parametrize("method", sorted(_OPERATIONS))
async def test_every_storage_operation_raises_a_backend_failure_as_a_storage_error(
    method: str, storage: AsyncSqliteStorage
) -> None:
    """The transport's storage raises any backend failure as one error, chained from the backend's own.

    Its message is the backend error's type alone, since a backend's text may carry its DSN and password.
    """
    fault = StorageTimeoutError("timeout at postgres://admin:hunter2@db")
    guarded = cache_transport(FaultyStorage(storage, fault, failing=method), Network()).storage
    entry_id = (await storage.create_entry(Request("GET", _URL), Response(httpx2.codes.OK), "key")).id

    with pytest.raises(CacheStorageError) as raised:
        await _OPERATIONS[method](guarded, entry_id)

    assert str(raised.value) == "StorageTimeoutError"
    assert raised.value.__cause__ is fault


async def test_the_guarded_storage_delegates_a_close_to_the_backend(storage: AsyncSqliteStorage) -> None:
    """The transport never closes its storage, but its guard still forwards a close to whoever asks for one."""
    backend = FaultyStorage(storage, StorageTimeoutError("closing"), failing="close")

    with pytest.raises(StorageTimeoutError):
        await cache_transport(backend, Network()).storage.close()


class _FailsWhileStoring(AsyncSqliteStorage):
    """A real SQLite storage whose write of a miss's body fails on the third chunk, with a DSN in the message."""

    @override
    async def _save_stream(self, stream: AsyncIterable[bytes], entry_id: bytes) -> AsyncIterator[bytes]:
        chunks = 0
        async for chunk in stream:
            chunks += 1
            if chunks == len(("first", "second", "third")):
                msg = "disk I/O error (db=postgres://admin:hunter2@db)"
                raise sqlite3.OperationalError(msg)
            yield chunk


@dataclasses.dataclass
class _Received:
    """How many bytes a stream yielded, kept outside the stream so a test sees what arrived before a failure."""

    total: int = 0

    async def drain(self, chunks: AsyncIterator[bytes]) -> None:
        """Count every byte of *chunks*."""
        async for chunk in chunks:
            self.total += len(chunk)


async def test_a_storage_fault_while_an_unheld_body_streams_is_a_value_free_transport_error() -> None:
    """Past the cap a body streams on through the storage's tee after the fallback has returned.

    A fault there fails the read as a ``TransportError`` naming only the backend error's type, never its text (a DSN
    with a password), and charges the host nothing, since the network did not fail.
    """
    store = _FailsWhileStoring()
    client = stub_client(
        transport=TrackingNetwork((b"x" * 10,) * 10),
        cache=HttpCache(policy=every_get_policy(), storage=store),
        http=HttpOptions(max_response_bytes=15),
    )
    received = _Received()
    try:
        with pytest.raises(TransportError) as raised:
            await received.drain(client.stream_get("big"))
    finally:
        await client.aclose()
        await store.close()

    assert received.total, "the head of the body was yielded before the fault"
    assert "OperationalError" in str(raised.value)
    assert "hunter2" not in str(raised.value)
    read_error = raised.value.__cause__
    assert isinstance(read_error, httpx2.ReadError)
    assert read_error.__cause__ is None, "the backend's own error is not chained"
    assert read_error.__suppress_context__
    assert host_untested(read_error)
    assert HOST_BREAKERS["x"].failure_count == 0


def test_an_unmarked_response_or_error_is_host_evidence() -> None:
    """Only what the cache marked says nothing about the host."""
    assert not host_untested(httpx2.Response(httpx2.codes.OK))
    assert not host_untested(httpx2.ConnectError("refused"))
    assert host_untested(httpx2.Response(httpx2.codes.OK, extensions={HOST_UNTESTED: True}))
