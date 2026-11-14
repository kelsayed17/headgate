# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""The ``CacheResult`` event each cached request emits: a hit, a miss, a degraded fetch, or a shared download."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, override

import httpx2

from headgate import Client, HttpOptions, TransportError, TransportPolicy
from headgate.cache import HttpCache
from headgate.cache._transport import served_from_cache
from headgate.events import CacheOutcome, CacheResult, Event
from tests.cache._helpers import FaultyStorage, Network, every_get_rules
from tests.support.responses import RESPONSE

if TYPE_CHECKING:
    from hishel import AsyncBaseStorage, AsyncSqliteStorage

_URL = "https://x/series"
_CONCURRENT = 3


class _Recorder:
    """An observer that keeps every ``CacheResult`` it receives."""

    def __init__(self) -> None:
        """Start empty."""
        self.outcomes: list[CacheOutcome] = []
        self.urls: list[str] = []

    def notify(self, event: Event) -> None:
        """Keep a cache event's outcome and URL."""
        if isinstance(event, CacheResult):
            self.outcomes.append(event.outcome)
            self.urls.append(event.call.url)


class _GatedNetwork(httpx2.AsyncBaseTransport):
    """The network leg: holds every fetch open until the test opens the gate, then answers or raises *exc*."""

    def __init__(self, exc: Exception | None = None) -> None:
        """Answer 200 once the gate opens, or raise *exc*."""
        self.fetching = asyncio.Event()
        self.gate = asyncio.Event()
        self._exc = exc

    @override
    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        self.fetching.set()
        await self.gate.wait()
        if self._exc is not None:
            raise self._exc
        return httpx2.Response(httpx2.codes.OK, content=b"body")


def _client(network: httpx2.AsyncBaseTransport, storage: AsyncBaseStorage, recorder: _Recorder) -> Client:
    """Build a client caching every GET into *storage*, reporting to *recorder*, retrying nothing."""
    return Client(
        base_url="https://x",
        policy=TransportPolicy(retry=None, cache=HttpCache(**every_get_rules(), storage=storage)),
        http=HttpOptions(transport=network),
        observers=(recorder,),
    )


async def _until_fetching(network: _GatedNetwork) -> None:
    """Wait until the first request reaches the network, then let the rest arrive and wait on it."""
    await network.fetching.wait()
    for _ in range(20):
        await asyncio.sleep(0)


async def test_a_miss_then_a_hit(storage: AsyncSqliteStorage) -> None:
    """The first GET is a miss answered by the network, the second a hit; each event names its call."""
    recorder = _Recorder()
    client = _client(Network(), storage, recorder)

    await client.get(_URL, expect=RESPONSE)
    await client.get(_URL, expect=RESPONSE)

    assert recorder.outcomes == [CacheOutcome.MISS, CacheOutcome.HIT]
    assert recorder.urls == [_URL, _URL]


async def test_a_storage_fault_is_degraded(storage: AsyncSqliteStorage) -> None:
    """A request that found the storage broken reports a degraded fetch, not a miss."""
    recorder = _Recorder()
    client = _client(Network(), FaultyStorage(storage, RuntimeError("down"), failing="get_entries"), recorder)

    await client.get(_URL, expect=RESPONSE)

    assert recorder.outcomes == [CacheOutcome.DEGRADED]


async def test_requests_that_waited_on_another_report_a_shared_download(storage: AsyncSqliteStorage) -> None:
    """Identical GETs in flight together: the one that fetched misses, every one that waited shares."""
    recorder = _Recorder()
    network = _GatedNetwork()
    client = _client(network, storage, recorder)

    tasks = [asyncio.create_task(client.get(_URL, expect=RESPONSE)) for _ in range(_CONCURRENT)]
    await _until_fetching(network)
    network.gate.set()
    await asyncio.gather(*tasks)

    assert sorted(recorder.outcomes) == sorted([CacheOutcome.MISS] + [CacheOutcome.SHARED] * (_CONCURRENT - 1))


async def test_requests_that_shared_a_failure_report_it_shared(storage: AsyncSqliteStorage) -> None:
    """A network failure shared with the waiting requests is reported as shared by each of them."""
    recorder = _Recorder()
    network = _GatedNetwork(httpx2.ConnectError("refused"))
    client = _client(network, storage, recorder)

    tasks = [asyncio.create_task(client.get(_URL, expect=RESPONSE)) for _ in range(_CONCURRENT)]
    await _until_fetching(network)
    network.gate.set()
    results = await asyncio.gather(*tasks, return_exceptions=True)

    assert all(isinstance(result, TransportError) for result in results)
    assert recorder.outcomes == [CacheOutcome.SHARED] * (_CONCURRENT - 1)


async def test_a_client_without_observers_emits_nothing(storage: AsyncSqliteStorage) -> None:
    """Without observers no event is built; the cache still works."""
    network = Network()
    client = Client(
        base_url="https://x",
        policy=TransportPolicy(cache=HttpCache(**every_get_rules(), storage=storage)),
        http=HttpOptions(transport=network),
    )

    await client.get(_URL, expect=RESPONSE)
    hit = await client.get(_URL, expect=RESPONSE)

    assert served_from_cache(hit)
    assert network.calls == 1


def test_every_outcome_is_one_the_cache_emits() -> None:
    """Offer only the outcomes the cache reports: it never revalidates, so there is no revalidated outcome."""
    assert {outcome.value for outcome in CacheOutcome} == {"hit", "miss", "shared", "degraded"}
