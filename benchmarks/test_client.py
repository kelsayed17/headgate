# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""End-to-end request cost through the full headgate stack, against an in-memory transport.

The network is an ``httpx2.MockTransport``, so what is measured is everything headgate adds: URL rendering, the retry,
cache and rate-limit transports, the circuit breaker, the size-capped body read, and the declared-response dispatch.
``test_bare_httpx2_get`` runs the same request through a plain ``httpx2.AsyncClient`` as the baseline.
"""

from __future__ import annotations

import asyncio
import dataclasses
import gzip
import itertools
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import TYPE_CHECKING, override

import httpx2
import msgspec
from hishel import AsyncBaseStorage, Entry, EntryMeta, FilterPolicy, Request, Response

from headgate import (
    AsyncBaseClient,
    HttpCache,
    HttpOptions,
    QueryParamAuth,
    Rate,
    RetryPolicy,
    TransportPolicy,
    as_bytes,
)
from headgate import msgspec as msgspec_flavor
from headgate._core import Timing, timing_override
from headgate.cache import MethodFilter, StatusCodeFilter

if TYPE_CHECKING:
    from collections.abc import Callable

    from pytest_codspeed import BenchmarkFixture

    from benchmarks.conftest import Operation

_BASE_URL = "https://api.example.test/v1/"
_FAST = Rate.per_second(1_000_000)
_CONCURRENCY = 100
_LARGE_BODY_BYTES = 8 * 1024 * 1024
_OK = httpx2.codes.OK
_UNAVAILABLE = httpx2.codes.SERVICE_UNAVAILABLE


class Series(msgspec.Struct, frozen=True):
    """The decoded body of every benchmark response."""

    series_id: str
    values: list[float]


_BODY = msgspec.json.encode(Series("GDP", [float(i) for i in range(50)]))
_SERIES = msgspec_flavor.decoder(Series)


def _ok(request: httpx2.Request) -> httpx2.Response:
    """Answer every request with the same small JSON body."""
    return httpx2.Response(_OK, content=_BODY, headers={"content-type": "application/json"}, request=request)


async def _replay(body: bytes) -> AsyncIterator[bytes]:
    """Yield a stored *body* as one chunk."""
    yield body


class _MemoryStorage(AsyncBaseStorage):
    """A dict-backed hishel storage, so a cache hit measures headgate's cache layer, not a database.

    hishel's SQLite storage runs every query on a worker thread; the thread handoffs are system calls, which CodSpeed's
    CPU simulation cannot measure, and their cost is the backend's, not headgate's.
    """

    def __init__(self) -> None:
        """Start empty."""
        self._entries: dict[str, list[tuple[Entry, bytes]]] = {}

    @override
    async def create_entry(self, request: Request, response: Response, key: str, id_: uuid.UUID | None = None) -> Entry:
        entry = Entry(
            id=uuid.uuid4() if id_ is None else id_,
            request=request,
            meta=EntryMeta(),
            response=response,
            cache_key=key.encode(),
        )
        stream = response.stream
        if isinstance(stream, Iterator):
            msg = "an async storage was handed a synchronous body"
            raise TypeError(msg)

        async def stored() -> AsyncIterator[bytes]:
            # Kept once the body has streamed through, as a real storage writes it.
            chunks = [chunk async for chunk in stream]
            self._entries.setdefault(key, []).append((entry, b"".join(chunks)))
            for chunk in chunks:
                yield chunk

        return dataclasses.replace(entry, response=dataclasses.replace(response, stream=stored()))

    @override
    async def get_entries(self, key: str) -> list[Entry]:
        return [
            dataclasses.replace(entry, response=dataclasses.replace(entry.response, stream=_replay(body)))
            for entry, body in self._entries.get(key, [])
        ]

    @override
    async def update_entry(self, id: uuid.UUID, new_entry: Entry | Callable[[Entry], Entry]) -> Entry | None:
        msg = "a cache hit never updates an entry"
        raise NotImplementedError(msg)

    @override
    async def remove_entry(self, id: uuid.UUID) -> None:
        msg = "a cache hit never removes an entry"
        raise NotImplementedError(msg)

    @override
    async def close(self) -> None:
        self._entries.clear()


def _client(
    handler: Callable[[httpx2.Request], httpx2.Response] = _ok,
    *,
    auth: httpx2.Auth | None = None,
    cache: HttpCache | None = None,
    breaker: bool = True,
) -> AsyncBaseClient:
    """Build a client over an in-memory transport with a rate and a retry budget that never throttle."""
    retry = RetryPolicy(budget=None)
    return AsyncBaseClient(
        base_url=_BASE_URL,
        rate=_FAST,
        auth=auth,
        policy=TransportPolicy(retry=retry, cache=cache)
        if breaker
        else TransportPolicy(retry=retry, cache=cache, breaker=None),
        http=HttpOptions(transport=httpx2.MockTransport(handler)),
    )


def test_bare_httpx2_get(run_batch: Callable[[Operation], None]) -> None:
    """Measure the baseline: the same GET and decode through a plain httpx2 client."""
    client = httpx2.AsyncClient(base_url=_BASE_URL, transport=httpx2.MockTransport(_ok))

    async def get() -> Series:
        response = await client.get("series")
        return msgspec.json.decode(response.content, type=Series)

    run_batch(get)


def test_get(run_batch: Callable[[Operation], None]) -> None:
    """Measure a decoded GET through the full stack."""
    client = _client()
    run_batch(lambda: client.get("series", expect=_SERIES))


def test_get_with_tstring_path(run_batch: Callable[[Operation], None]) -> None:
    """Measure a GET whose path is rendered from a t-string with two escaped segments."""
    client = _client()
    series_id, vintage = "GDP/real", "2026-10-24"
    run_batch(lambda: client.get(t"series/{series_id}/vintages/{vintage}", expect=_SERIES))


def test_get_with_query_param_auth(run_batch: Callable[[Operation], None]) -> None:
    """Measure a GET that carries a query-parameter credential and query parameters."""
    client = _client(auth=QueryParamAuth({"api_key": "k"}))
    run_batch(lambda: client.get("series", expect=_SERIES, params={"id": "GDP", "limit": 100}))


def test_get_without_circuit_breaker(run_batch: Callable[[Operation], None]) -> None:
    """Measure a GET with the breaker off, to isolate the breaker's share of the cost."""
    client = _client(breaker=False)
    run_batch(lambda: client.get("series", expect=_SERIES))


async def _no_wait(_seconds: float, /) -> None:
    """Yield to the event loop instead of sleeping, as a zero-second backoff would."""
    await asyncio.sleep(0)


def test_get_retried_once(run_batch: Callable[[Operation], None]) -> None:
    """Measure a GET that is answered 503 once and then retried, with the backoff sleep returning at once."""
    sent = itertools.count()

    def flaky(request: httpx2.Request) -> httpx2.Response:
        if next(sent) % 2 == 0:
            return httpx2.Response(_UNAVAILABLE, request=request)
        return _ok(request)

    with timing_override(Timing(sleep=_no_wait)):
        client = _client(flaky)
    run_batch(lambda: client.get("series", expect=_SERIES))


def test_get_cache_hit(runner: asyncio.Runner, run_batch: Callable[[Operation], None]) -> None:
    """Measure a GET served from the HTTP cache, from a dict-backed storage (see :class:`_MemoryStorage`)."""
    storage = _MemoryStorage()
    policy = FilterPolicy(
        request_filters=[MethodFilter(frozenset({"GET"}))],
        response_filters=[StatusCodeFilter(frozenset({_OK}))],
    )
    client = _client(cache=HttpCache(policy=policy, storage=storage, ttl=3600))
    try:
        run_batch(lambda: client.get("series", expect=_SERIES))
    finally:
        # The client never closes a storage, which other clients may share: whoever opened it closes it.
        runner.run(client.aclose())
        runner.run(storage.close())


def test_concurrent_gets(benchmark: BenchmarkFixture, runner: asyncio.Runner) -> None:
    """Measure ``_CONCURRENCY`` GETs in flight at once."""
    client = _client()

    async def burst() -> None:
        await asyncio.gather(*(client.get("series", expect=_SERIES) for _ in range(_CONCURRENCY)))

    runner.run(burst())
    benchmark(lambda: runner.run(burst()))


def test_large_gzip_body(benchmark: BenchmarkFixture, runner: asyncio.Runner) -> None:
    """Measure reading a gzip body that decompresses to 8 MiB under the size cap."""
    payload = gzip.compress(b"x" * _LARGE_BODY_BYTES)

    def gzipped(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(_OK, content=payload, headers={"content-encoding": "gzip"}, request=request)

    client = _client(gzipped)
    benchmark(lambda: runner.run(client.get("file", expect=as_bytes)))


def test_stream(benchmark: BenchmarkFixture, runner: asyncio.Runner) -> None:
    """Measure streaming an 8 MiB body chunk by chunk."""
    payload = b"x" * _LARGE_BODY_BYTES
    client = _client(lambda request: httpx2.Response(_OK, content=payload, request=request))

    async def drain() -> int:
        async with client.stream("GET", "file") as stream:
            return sum([len(chunk) async for chunk in stream.aiter_bytes()])

    benchmark(lambda: runner.run(drain()))
