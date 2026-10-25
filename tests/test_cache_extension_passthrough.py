"""A cache-enabled client must keep its TIMEOUT (and anything else it threads).

hishel does not forward the request it was given: it converts to its own ``Request``, keeps
only the metadata it recognises (``hishel_ttl`` and friends), and rebuilds an
``httpx2.Request`` from that before calling the transport below. Everything else in
``request.extensions`` is dropped — **including httpx2's own ``timeout``**, which is not a
client setting that travels some other way but the per-request extension httpx2 builds from
``AsyncClient(timeout=...)`` and the only thing httpcore reads (``request.extensions.get(
"timeout", {})``).

Measured before the fix, against a socket that accepts and never answers: a plain client
raised ``ClientTimeoutError`` after its configured 2.0s; a cache-enabled client was still
waiting when an outer 15s guard killed it. Connect, read, write and pool were all absent, so
a cache MISS to a black-holing host hung until something else gave up — which for an agent
cycle is the cycle's own deadline, if it has one.

The fix restores the original request's extensions below the cache. These tests assert the
mechanism (the extension arrives at the network transport, and a per-call ``timeout=``
override still wins) and the consequence (the timeout actually fires), the second against a
real socket, because nothing short of one exercises httpcore's timeout at all.
"""

from __future__ import annotations

import socket
import time
from typing import TYPE_CHECKING, override

import anysqlite
import httpx2
import pytest
from hishel import AsyncSqliteStorage, FilterPolicy

from headgate._base import AsyncBaseClient
from headgate._cache import HttpCache, PathFragmentFilter, StatusCodeFilter
from headgate._rate_limit import UNLIMITED
from headgate.errors import ClientTimeoutError
from tests.support.responses import RESPONSE

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


class _StubClient(AsyncBaseClient):
    """Minimal AsyncBaseClient subclass for cache-passthrough tests."""


class _RecordingTransport(httpx2.AsyncBaseTransport):
    """Records the extensions each request carries when it reaches the network."""

    def __init__(self) -> None:
        self.extensions: list[dict[str, object]] = []

    @override
    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        self.extensions.append(dict(request.extensions))
        return httpx2.Response(200, content=b"{}", headers={"Cache-Control": "max-age=3600"})


@pytest.fixture
async def sqlite_storage() -> AsyncIterator[AsyncSqliteStorage]:
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


async def test_the_timeout_survives_the_cache_layer(sqlite_storage: AsyncSqliteStorage) -> None:
    base = _RecordingTransport()
    client = _StubClient(
        base_url="https://x",
        transport=base,
        rate=UNLIMITED,
        timeout=7.0,
        cache=HttpCache(policy=_series_policy(), storage=sqlite_storage),
    )

    await client.get("/series?id=A", expect=RESPONSE)

    assert base.extensions, "the request must reach the network on a MISS"
    timeout = base.extensions[0].get("timeout")
    assert timeout is not None, "httpcore reads the timeout from extensions; without it there is NO timeout"
    assert timeout == {"connect": 10.0, "read": 7.0, "write": 10.0, "pool": 15.0}


async def test_a_per_call_timeout_still_wins_through_the_cache(sqlite_storage: AsyncSqliteStorage) -> None:
    """Restoring must replay THIS request's extensions, not a value captured at construction."""
    base = _RecordingTransport()
    client = _StubClient(
        base_url="https://x",
        transport=base,
        rate=UNLIMITED,
        timeout=7.0,
        cache=HttpCache(policy=_series_policy(), storage=sqlite_storage),
    )

    await client.get("/series?id=B", expect=RESPONSE, timeout=1.5)

    assert base.extensions[0]["timeout"] == {"connect": 1.5, "read": 1.5, "write": 1.5, "pool": 1.5}


async def test_the_cache_layer_keeps_its_own_extension(sqlite_storage: AsyncSqliteStorage) -> None:
    """Restoring the original must not clobber what hishel computed for itself."""
    base = _RecordingTransport()
    client = _StubClient(
        base_url="https://x",
        transport=base,
        rate=UNLIMITED,
        cache=HttpCache(policy=_series_policy(), storage=sqlite_storage, ttl=3600.0),
    )

    await client.get("/series?id=C", expect=RESPONSE)

    assert base.extensions[0].get("hishel_ttl") == 3600.0


async def test_a_black_holing_host_times_out_through_the_cache(sqlite_storage: AsyncSqliteStorage) -> None:
    """The consequence, over a real socket — the only way httpcore's timeout is exercised at all."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(4)  # accepts, never answers
    port = listener.getsockname()[1]

    client = _StubClient(
        base_url=f"http://127.0.0.1:{port}",
        rate=UNLIMITED,
        timeout=0.25,
        max_retries=1,
        cache=HttpCache(policy=_series_policy(), storage=sqlite_storage),
    )
    started = time.monotonic()
    try:
        with pytest.raises(ClientTimeoutError):
            await client.get("/series?id=D", expect=RESPONSE)
    finally:
        await client.aclose()
        listener.close()

    assert time.monotonic() - started < 5.0, "the client's own 0.25s timeout must be what ends this"
