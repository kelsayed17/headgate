# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""Memory stays bounded: streamed bodies are never held whole, and repeated requests retain nothing.

These measure allocations with ``tracemalloc`` rather than wall-clock time, so they are deterministic enough to run in
the regular suite. Timing lives in ``benchmarks/``.
"""

from __future__ import annotations

import asyncio
import gc
import itertools
import tracemalloc
from typing import TYPE_CHECKING, override

import anysqlite
import httpx2
import pytest
from hishel import AsyncSqliteStorage, FilterPolicy

from headgate import ClientError, HttpCache, HttpOptions, RetryPolicy, TransportPolicy, as_bytes
from headgate.cache import MethodFilter, StatusCodeFilter
from headgate.ratelimit import Rate
from tests.support.clients import StubClient

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

_MIB = 1024 * 1024
_CHUNK = 64 * 1024
_STREAMED_BYTES = 64 * _MIB
#: The most a streamed 64 MiB body may hold at once: a handful of chunks, never the body.
_STREAM_PEAK_LIMIT = 4 * _MIB
#: Requests run before measuring, untraced: enough for every bounded cache below (hishel's storage, regex and URL
#: caches) to fill, so a leak shows as growth that continues rather than a cache that plateaus.
_WARMUP_REQUESTS = 2500
#: Requests per traced window: one to settle what tracing itself allocates, then one that is measured.
_WINDOW_REQUESTS = 500
#: The most one measured window may leave allocated after a full collection: under 128 bytes a request, less than any
#: object a leak would retain per request.
_GROWTH_LIMIT = 64 * 1024
_FAST = Rate.per_second(1_000_000)
_BODY = b'{"series_id": "GDP", "values": [1.0, 2.0, 3.0]}'


class _Chunks(httpx2.AsyncByteStream):
    """A response body produced chunk by chunk, so the transport itself never holds it whole."""

    def __init__(self, total: int) -> None:
        self._total = total

    @override
    async def __aiter__(self) -> AsyncIterator[bytes]:
        chunk = b"x" * _CHUNK
        for _ in range(self._total // _CHUNK):
            yield chunk


def _ok(request: httpx2.Request) -> httpx2.Response:
    """Answer with a small body."""
    return httpx2.Response(httpx2.codes.OK, content=_BODY, request=request)


def _client(handler: Callable[[httpx2.Request], httpx2.Response] = _ok, cache: HttpCache | None = None) -> StubClient:
    """Build a client over an in-memory transport that never throttles, with no retry budget to run out of."""
    return StubClient(
        base_url="https://api.example.test",
        rate=_FAST,
        policy=TransportPolicy(retry=RetryPolicy(budget=None), cache=cache),
        http=HttpOptions(transport=httpx2.MockTransport(handler)),
    )


async def _growth_per_window(operation: Callable[[], Awaitable[object]]) -> int:
    """Return how many bytes a window of *operation* runs leaves allocated, once warmed up.

    A leak retains something on every run, so it grows window after window; a bounded cache fills during the warmup
    and then stops. Measuring a window after the warmup tells the two apart.

    The event loop turns once before each snapshot, as it does between requests in any real program. An in-memory
    transport never suspends, so without that turn the loop never purges the deadline timers each call cancels, which
    it does whenever it runs.
    """
    for _ in range(_WARMUP_REQUESTS):
        await operation()
    tracemalloc.start()
    try:
        for _ in range(_WINDOW_REQUESTS):
            await operation()
        await asyncio.sleep(0)
        gc.collect()
        before = tracemalloc.take_snapshot()
        for _ in range(_WINDOW_REQUESTS):
            await operation()
        await asyncio.sleep(0)
        gc.collect()
        after = tracemalloc.take_snapshot()
    finally:
        tracemalloc.stop()
    return sum(stat.size_diff for stat in after.compare_to(before, "filename"))


async def test_streaming_a_large_body_holds_a_few_chunks_at_most() -> None:
    """Stream 64 MiB through ``stream()`` while holding no more than a few MiB at any moment."""
    client = _client(lambda request: httpx2.Response(httpx2.codes.OK, stream=_Chunks(_STREAMED_BYTES), request=request))
    tracemalloc.start()
    try:
        received = 0
        async with client.stream("file") as stream:
            async for chunk in stream.aiter_bytes():
                received += len(chunk)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert received == _STREAMED_BYTES
    assert peak < _STREAM_PEAK_LIMIT, f"peak {peak / _MIB:.1f} MiB while streaming"


async def test_repeated_gets_retain_nothing() -> None:
    """Retain nothing per request across thousands of successful GETs."""
    client = _client()
    assert await _growth_per_window(lambda: client.get("series", expect=as_bytes)) < _GROWTH_LIMIT


async def test_repeated_failures_retain_nothing() -> None:
    """Retain nothing per request across thousands of GETs that raise, so tracebacks and errors are not kept alive."""
    client = _client(lambda request: httpx2.Response(httpx2.codes.NOT_FOUND, text="no such series", request=request))

    async def failing_get() -> None:
        with pytest.raises(ClientError):
            await client.get("series", expect=as_bytes)

    assert await _growth_per_window(failing_get) < _GROWTH_LIMIT


async def test_repeated_retries_retain_nothing() -> None:
    """Retain nothing per request across thousands of GETs that are answered 503 once and then retried."""
    sent = itertools.count()

    def flaky(request: httpx2.Request) -> httpx2.Response:
        if next(sent) % 2 == 0:
            return httpx2.Response(httpx2.codes.SERVICE_UNAVAILABLE, request=request)
        return _ok(request)

    client = _client(flaky)
    assert await _growth_per_window(lambda: client.get("series", expect=as_bytes)) < _GROWTH_LIMIT


async def test_repeated_cache_hits_retain_nothing() -> None:
    """Retain nothing per request across thousands of GETs served from the HTTP cache."""
    storage = AsyncSqliteStorage(connection=await anysqlite.connect(":memory:"))
    policy = FilterPolicy(
        request_filters=[MethodFilter(frozenset({"GET"}))],
        response_filters=[StatusCodeFilter(frozenset({httpx2.codes.OK}))],
    )
    client = _client(cache=HttpCache(policy=policy, storage=storage, ttl=3600))
    try:
        assert await _growth_per_window(lambda: client.get("series", expect=as_bytes)) < _GROWTH_LIMIT
    finally:
        await storage.close()


async def test_repeated_streams_retain_nothing() -> None:
    """Retain nothing per request across thousands of small streamed GETs."""
    client = _client()

    async def drain() -> None:
        async with client.stream("file") as stream:
            async for _ in stream.aiter_bytes():
                pass

    assert await _growth_per_window(drain) < _GROWTH_LIMIT
