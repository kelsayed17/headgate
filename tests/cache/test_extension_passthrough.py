# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""A cache-enabled client keeps its timeout, and every other request extension, below the cache.

hishel does not forward the request it was given: it converts it to its own ``Request``, keeps only the metadata it
recognises (``hishel_ttl`` and friends), and rebuilds an ``httpx2.Request`` from that for the transport below. The rest
of ``request.extensions`` is dropped, including httpx2's ``timeout``: the per-request extension httpx2 builds from
``AsyncClient(timeout=...)`` and the only place httpcore reads a timeout from. Without it a cache MISS to a host that
accepts and never answers waits forever.

``ResilientCacheTransport`` restores the original request's extensions below the cache. These tests pin the mechanism
(the extension reaches the network transport, and a per-call ``timeout=`` still wins) and the consequence (the timeout
fires), the latter over a real socket, since nothing short of one exercises httpcore's timeout.
"""

from __future__ import annotations

import socket
import time
from typing import TYPE_CHECKING, override

import httpx2
import pytest

from headgate._cache import HttpCache
from headgate._rate_limit import UNLIMITED
from headgate.errors import ClientTimeoutError
from tests.cache._helpers import series_policy
from tests.support.clients import StubClient
from tests.support.responses import RESPONSE

if TYPE_CHECKING:
    from hishel import AsyncSqliteStorage


class _RecordingTransport(httpx2.AsyncBaseTransport):
    """Records the extensions each request carries when it reaches the network."""

    def __init__(self) -> None:
        self.extensions: list[dict[str, object]] = []

    @override
    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        self.extensions.append(dict(request.extensions))
        return httpx2.Response(200, content=b"{}", headers={"Cache-Control": "max-age=3600"})


async def test_the_timeout_survives_the_cache_layer(storage: AsyncSqliteStorage) -> None:
    base = _RecordingTransport()
    client = StubClient(
        base_url="https://x",
        transport=base,
        rate=UNLIMITED,
        timeout=7.0,
        cache=HttpCache(policy=series_policy(), storage=storage),
    )

    await client.get("/series?id=A", expect=RESPONSE)

    assert base.extensions, "the request must reach the network on a MISS"
    timeout = base.extensions[0].get("timeout")
    assert timeout is not None, "httpcore reads its timeout only from the request extensions"
    assert timeout == {"connect": 10.0, "read": 7.0, "write": 10.0, "pool": 15.0}


async def test_a_per_call_timeout_still_wins_through_the_cache(storage: AsyncSqliteStorage) -> None:
    """Restoring must replay THIS request's extensions, not a value captured at construction."""
    base = _RecordingTransport()
    client = StubClient(
        base_url="https://x",
        transport=base,
        rate=UNLIMITED,
        timeout=7.0,
        cache=HttpCache(policy=series_policy(), storage=storage),
    )

    await client.get("/series?id=B", expect=RESPONSE, timeout=1.5)

    assert base.extensions[0]["timeout"] == {"connect": 1.5, "read": 1.5, "write": 1.5, "pool": 1.5}


async def test_the_cache_layer_keeps_its_own_extension(storage: AsyncSqliteStorage) -> None:
    """Restoring the original must not clobber what hishel computed for itself."""
    base = _RecordingTransport()
    client = StubClient(
        base_url="https://x",
        transport=base,
        rate=UNLIMITED,
        cache=HttpCache(policy=series_policy(), storage=storage, ttl=3600.0),
    )

    await client.get("/series?id=C", expect=RESPONSE)

    assert base.extensions[0].get("hishel_ttl") == 3600.0


async def test_a_black_holing_host_times_out_through_the_cache(storage: AsyncSqliteStorage) -> None:
    """The consequence, over a real socket: a host that accepts and never answers hits the client's timeout."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(4)  # accepts, never answers
    port = listener.getsockname()[1]

    client = StubClient(
        base_url=f"http://127.0.0.1:{port}",
        rate=UNLIMITED,
        timeout=0.25,
        max_attempts=1,
        cache=HttpCache(policy=series_policy(), storage=storage),
    )
    started = time.monotonic()
    try:
        with pytest.raises(ClientTimeoutError):
            await client.get("/series?id=D", expect=RESPONSE)
    finally:
        await client.aclose()
        listener.close()

    assert time.monotonic() - started < 5.0, "the client's own 0.25s timeout must be what ends this"
