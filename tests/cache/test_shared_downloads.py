""":class:`ResilientCacheTransport`: simultaneous identical GETs share one download.

hishel has no request coalescing: while one request for a file is on its way to the network, an identical request finds
nothing in storage and goes too, so five concurrent lookups of one large file on a cold cache would make five
downloads. The first request fetches, and every identical GET that arrives while it is in flight waits for it and gets
its own copy of the response.

Driven through the real hishel machinery (an in-memory SQLite storage), with a network leg that holds each fetch open
until the test releases it, so the requests genuinely overlap.
"""

from __future__ import annotations

import asyncio
import gc
import logging
from typing import TYPE_CHECKING, override

import httpx2
import pytest
from hishel import FilterPolicy

from headgate._cache import MethodFilter, ResilientCacheTransport
from tests.cache._helpers import every_get_policy

if TYPE_CHECKING:
    from collections.abc import Callable

    from hishel import AsyncSqliteStorage

_URL = "https://example.test/files/2025.zip"
_BODY = b"the whole file"


class _GatedNetwork(httpx2.AsyncBaseTransport):
    """The network leg: counts fetches, and holds each one open until the test opens the gate."""

    def __init__(self, *, body: bytes = _BODY, exc: Exception | None = None) -> None:
        self.calls = 0
        self.gate = asyncio.Event()
        self._body = body
        self._exc = exc

    @override
    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        self.calls += 1
        await self.gate.wait()
        if self._exc is not None:
            raise self._exc
        return httpx2.Response(200, content=self._body, request=request)


def _cache_no_get() -> FilterPolicy:
    return FilterPolicy(request_filters=[MethodFilter(frozenset({"POST"}))])


def _transport(
    storage: AsyncSqliteStorage,
    network: httpx2.AsyncBaseTransport,
    *,
    policy: FilterPolicy | None = None,
    cap: int = 1024 * 1024,
) -> ResilientCacheTransport:
    return ResilientCacheTransport(
        next_transport=network,
        storage=storage,
        policy=policy or every_get_policy(),
        max_body_bytes=cap,
    )


async def _get(transport: ResilientCacheTransport, url: str = _URL) -> bytes:
    response = await transport.handle_async_request(httpx2.Request("GET", url))
    return await response.aread()


async def _eventually(condition: Callable[[], bool]) -> None:
    """Wait until *condition* holds; the storage runs on a worker thread, so requests reach the network in real time."""
    for _ in range(400):
        if condition():
            return
        await asyncio.sleep(0.005)
    msg = "the condition never held"
    raise AssertionError(msg)


async def _let_waiters_arrive() -> None:
    """Yield until every task already started has reached the transport."""
    for _ in range(20):
        await asyncio.sleep(0)


async def test_simultaneous_identical_gets_share_one_download(storage: AsyncSqliteStorage) -> None:
    network = _GatedNetwork()
    transport = _transport(storage, network)

    tasks = [asyncio.create_task(transport.handle_async_request(httpx2.Request("GET", _URL))) for _ in range(5)]
    await _eventually(lambda: network.calls == 1)
    await _let_waiters_arrive()
    network.gate.set()
    responses = await asyncio.gather(*tasks)

    assert network.calls == 1
    assert len({id(response) for response in responses}) == 5, "each request gets its own response"
    assert [await response.aread() for response in responses] == [_BODY] * 5
    assert {response.status_code for response in responses} == {200}


async def test_a_get_the_policy_does_not_cache_is_shared_too_and_only_while_in_flight(
    storage: AsyncSqliteStorage,
) -> None:
    """Sharing needs no cache entry: simultaneous requests for an uncached file share the one download.

    A request after it finished fetches again, since nothing stored it and the finished download is not handed out
    twice.
    """
    network = _GatedNetwork()
    transport = _transport(storage, network, policy=_cache_no_get())

    tasks = [asyncio.create_task(_get(transport)) for _ in range(5)]
    await _eventually(lambda: network.calls == 1)
    await _let_waiters_arrive()
    network.gate.set()

    assert await asyncio.gather(*tasks) == [_BODY] * 5
    assert network.calls == 1
    assert await _get(transport) == _BODY
    assert network.calls == 2


async def test_requests_for_different_urls_do_not_share(storage: AsyncSqliteStorage) -> None:
    network = _GatedNetwork()
    transport = _transport(storage, network)

    tasks = [asyncio.create_task(_get(transport, url)) for url in (_URL, _URL.replace("2025", "2024"))]
    await _eventually(lambda: network.calls == 2)
    network.gate.set()

    assert await asyncio.gather(*tasks) == [_BODY, _BODY]
    assert network.calls == 2


async def test_requests_that_differ_only_in_their_query_do_not_share(storage: AsyncSqliteStorage) -> None:
    """One path asked for a different ``reportDate`` each day is two requests: the query is part of what is asked for."""
    network = _GatedNetwork()
    transport = _transport(storage, network)

    days = ("https://example.test/download?reportDate=20260917", "https://example.test/download?reportDate=20260918")
    tasks = [asyncio.create_task(_get(transport, url)) for url in days]
    await _eventually(lambda: network.calls == 2)
    network.gate.set()

    assert await asyncio.gather(*tasks) == [_BODY, _BODY]
    assert network.calls == 2


async def test_posts_are_never_shared(storage: AsyncSqliteStorage) -> None:
    """A POST's body is part of what it asks for, so two identical-looking POSTs are two requests."""
    network = _GatedNetwork()
    transport = _transport(storage, network)

    async def post() -> bytes:
        response = await transport.handle_async_request(httpx2.Request("POST", _URL, content=b"{}"))
        return await response.aread()

    tasks = [asyncio.create_task(post()) for _ in range(2)]
    await _eventually(lambda: network.calls == 2)
    network.gate.set()

    assert await asyncio.gather(*tasks) == [_BODY, _BODY]
    assert network.calls == 2


async def test_a_failed_download_fails_every_waiting_request_from_one_attempt(storage: AsyncSqliteStorage) -> None:
    """Each caller's retry layer, above this one, then retries, and those retries share again."""
    network = _GatedNetwork(exc=httpx2.ConnectError("dns failure"))
    transport = _transport(storage, network)

    tasks = [asyncio.create_task(_get(transport)) for _ in range(5)]
    await _eventually(lambda: network.calls == 1)
    await _let_waiters_arrive()
    network.gate.set()
    results = await asyncio.gather(*tasks, return_exceptions=True)

    assert all(isinstance(result, httpx2.ConnectError) for result in results), results
    assert network.calls == 1


async def test_a_cancelled_first_request_hands_the_download_to_the_next(storage: AsyncSqliteStorage) -> None:
    network = _GatedNetwork()
    transport = _transport(storage, network)

    first = asyncio.create_task(_get(transport))
    await _eventually(lambda: network.calls == 1)
    waiting = [asyncio.create_task(_get(transport)) for _ in range(3)]
    await _let_waiters_arrive()
    first.cancel()
    await _eventually(lambda: network.calls == 2)
    network.gate.set()

    assert await asyncio.gather(*waiting) == [_BODY] * 3
    with pytest.raises(asyncio.CancelledError):
        await first
    assert network.calls == 2, "the three waiting requests shared the second download"


async def test_a_cancelled_waiting_request_does_not_cancel_the_download(storage: AsyncSqliteStorage) -> None:
    network = _GatedNetwork()
    transport = _transport(storage, network)

    first = asyncio.create_task(_get(transport))
    await _eventually(lambda: network.calls == 1)
    waiting = [asyncio.create_task(_get(transport)) for _ in range(3)]
    await _let_waiters_arrive()
    waiting[0].cancel()
    await _let_waiters_arrive()
    network.gate.set()

    assert await first == _BODY
    assert await asyncio.gather(*waiting[1:]) == [_BODY, _BODY]
    with pytest.raises(asyncio.CancelledError):
        await waiting[0]
    assert network.calls == 1


class _OverCapNetwork(httpx2.AsyncBaseTransport):
    """The first fetch answers at once; every later one waits until two are in flight together, then answers."""

    def __init__(self, body: bytes) -> None:
        self.calls = 0
        self._body = body
        self._later = 0
        self._together = asyncio.Event()

    @override
    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        self.calls += 1
        if self.calls > 1:
            self._later += 1
            if self._later == 2:
                self._together.set()
            await self._together.wait()
        return httpx2.Response(200, content=self._body, request=request)


async def test_a_response_too_large_to_hold_is_not_shared(storage: AsyncSqliteStorage) -> None:
    """Past the cap the body is not held, so there is nothing to share.

    The requests that waited each fetch their own, at the same time, and each client then raises its own too-large
    error, as it would without sharing.
    """
    network = _OverCapNetwork(b"0123456789")
    transport = _transport(storage, network, cap=4)

    tasks = [asyncio.create_task(_get(transport)) for _ in range(3)]
    bodies = await asyncio.wait_for(asyncio.gather(*tasks), timeout=5)

    assert bodies == [b"0123456789"] * 3
    assert network.calls == 3


class _Unreachable(httpx2.AsyncBaseTransport):
    """A network leg that fails every fetch with a fresh error, so no test object keeps the failure alive."""

    @override
    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        msg = "dns failure"
        raise httpx2.ConnectError(msg, request=request)


async def test_a_failure_no_request_waited_on_is_not_reported_as_never_retrieved(
    storage: AsyncSqliteStorage, caplog: pytest.LogCaptureFixture
) -> None:
    """A lone failed GET leaves no future whose exception nobody retrieved, which asyncio would log when collected."""
    transport = _transport(storage, _Unreachable())

    with caplog.at_level(logging.ERROR, logger="asyncio"):
        with pytest.raises(httpx2.ConnectError):
            await _get(transport)
        gc.collect()

    assert "never retrieved" not in caplog.text
