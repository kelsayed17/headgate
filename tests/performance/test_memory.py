# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""Memory stays bounded: streamed bodies are never held whole, and repeated requests retain nothing.

A successful call leaves no reference cycle either, so what it allocated is freed when it ends rather than at the next
garbage collection, which would raise the peak a stream of calls holds.

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
from hishel import AsyncSqliteStorage

from headgate import Client, ClientError, HttpCache, HttpOptions, Rate, RetryPolicy, TransportPolicy, as_bytes
from tests.support.events import Recorder

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
#: Frames kept per allocation, so a window past the limit can name the code that retained what grew.
_TRACE_FRAMES = 16
#: How many of the largest growths a window past the limit reports.
_REPORTED_GROWTHS = 5
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


def _client(
    handler: Callable[[httpx2.Request], httpx2.Response] = _ok,
    cache: HttpCache | None = None,
    *,
    observed: bool = False,
) -> Client:
    """Build a client over an in-memory transport that never throttles, with no retry budget to run out of.

    An *observed* client has an observer, which makes every call build its events and its breaker listener.
    """
    return Client(
        base_url="https://api.example.test",
        rate=_FAST,
        policy=TransportPolicy(retry=RetryPolicy(budget=None), cache=cache),
        http=HttpOptions(transport=httpx2.MockTransport(handler)),
        observers=(Recorder(),) if observed else (),
    )


async def _assert_no_growth(operation: Callable[[], Awaitable[object]]) -> None:
    """Assert that a window of *operation* runs, once warmed up, leaves less than :data:`_GROWTH_LIMIT` allocated.

    A leak retains something on every run, so it grows window after window; a bounded cache fills during the warmup
    and then stops. Measuring a window after the warmup tells the two apart.

    The event loop turns once before each snapshot, as it does between requests in any real program. An in-memory
    transport never suspends, so without that turn the loop never purges the deadline timers each call cancels, which
    it does whenever it runs.

    The failure names where the largest growths were allocated, since a leak seen on one platform only is diagnosed
    from that alone.
    """
    for _ in range(_WARMUP_REQUESTS):
        await operation()
    tracemalloc.start(_TRACE_FRAMES)
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
    growth = sum(stat.size_diff for stat in after.compare_to(before, "filename"))
    report = _report(growth, after.compare_to(before, "traceback")[:_REPORTED_GROWTHS])
    assert growth < _GROWTH_LIMIT, report


def _report(growth: int, stats: list[tracemalloc.StatisticDiff]) -> str:
    """Return what a window that grew by *growth* bytes says: the largest growths, where allocated, and the timers."""
    lines = [f"a window grew by {growth} B"]
    timers = getattr(asyncio.get_running_loop(), "_scheduled", [])
    lines.append(f"event loop: {len(timers)} timers scheduled, {sum(timer.cancelled() for timer in timers)} cancelled")
    for stat in stats:
        lines.append(f"+{stat.size_diff} B in {stat.count_diff} blocks, allocated at (innermost first):")
        lines.extend(f"    {line}" for line in stat.traceback.format(most_recent_first=True))
    return "\n".join(lines)


async def _garbage_per_call(operation: Callable[[], Awaitable[object]]) -> int:
    """Return how many objects a run of *operation* leaves only the garbage collector can free: those in cycles."""
    await operation()
    gc.collect()
    gc.disable()
    try:
        await operation()
        return gc.collect()
    finally:
        gc.enable()


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
    await _assert_no_growth(lambda: client.get("series", expect=as_bytes))


async def test_a_get_leaves_no_reference_cycle() -> None:
    """Free everything a successful GET allocated when it returns, by reference counting alone."""
    client = _client()
    assert await _garbage_per_call(lambda: client.get("series", expect=as_bytes)) == 0


async def test_an_observed_get_leaves_no_reference_cycle() -> None:
    """Free everything an observed GET allocated, its breaker listener and the gate its context holds included."""
    client = _client(observed=True)
    assert await _garbage_per_call(lambda: client.get("series", expect=as_bytes)) == 0


async def test_repeated_failures_retain_nothing() -> None:
    """Retain nothing per request across thousands of GETs that raise, so tracebacks and errors are not kept alive."""
    client = _client(lambda request: httpx2.Response(httpx2.codes.NOT_FOUND, text="no such series", request=request))

    async def failing_get() -> None:
        with pytest.raises(ClientError):
            await client.get("series", expect=as_bytes)

    await _assert_no_growth(failing_get)


async def test_repeated_retries_retain_nothing() -> None:
    """Retain nothing per request across thousands of GETs that are answered 503 once and then retried."""
    sent = itertools.count()

    def flaky(request: httpx2.Request) -> httpx2.Response:
        if next(sent) % 2 == 0:
            return httpx2.Response(httpx2.codes.SERVICE_UNAVAILABLE, request=request)
        return _ok(request)

    client = _client(flaky)
    await _assert_no_growth(lambda: client.get("series", expect=as_bytes))


async def test_repeated_cache_hits_retain_nothing() -> None:
    """Retain nothing per request across thousands of GETs served from the HTTP cache."""
    storage = AsyncSqliteStorage(connection=await anysqlite.connect(":memory:"))
    client = _client(cache=HttpCache(storage=storage, ttl=3600))
    try:
        await _assert_no_growth(lambda: client.get("series", expect=as_bytes))
    finally:
        await storage.close()


async def test_a_cache_hit_leaves_no_reference_cycle() -> None:
    """Free everything a GET served from the HTTP cache allocated when it returns, by reference counting alone."""
    storage = AsyncSqliteStorage(connection=await anysqlite.connect(":memory:"))
    client = _client(cache=HttpCache(storage=storage, ttl=3600))
    try:
        assert await _garbage_per_call(lambda: client.get("series", expect=as_bytes)) == 0
    finally:
        await storage.close()


async def test_repeated_streams_retain_nothing() -> None:
    """Retain nothing per request across thousands of small streamed GETs."""
    client = _client()

    async def drain() -> None:
        async with client.stream("file") as stream:
            async for _ in stream.aiter_bytes():
                pass

    await _assert_no_growth(drain)
