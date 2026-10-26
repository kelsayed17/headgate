# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

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

from headgate import HttpCache, HttpOptions, TransportError, TransportPolicy, UpstreamStatusError
from headgate._core import CallContext, CallInfo
from headgate._responses import as_bytes
from headgate.cache import MethodFilter
from headgate.cache._transport import CACHE_DEGRADED_EXT, host_untested
from headgate.ratelimit import UNLIMITED
from headgate.retry import NO_RETRY, RetryPolicy
from headgate.retry._policy import RETRY_POLICY
from tests.cache._helpers import FaultyStorage, TrackingNetwork, cache_transport, every_get_policy
from tests.support.breakers import failures
from tests.support.clients import StubClient

if TYPE_CHECKING:
    from collections.abc import Callable

    from hishel import AsyncSqliteStorage

    from headgate.cache._transport import ResilientCacheTransport

_URL = "https://example.test/files/2025.zip"
_BODY = b"the whole file"
#: How many identical requests a sharing test sends at once.
_CONCURRENT = 5
#: How many requests wait on a first one that a test then cancels.
_WAITING = 3


class _GatedNetwork(httpx2.AsyncBaseTransport):
    """The network leg: counts fetches, and holds each one open until the test opens the gate."""

    def __init__(self, *, body: bytes = _BODY, exc: Exception | None = None, status: int = httpx2.codes.OK) -> None:
        self.calls = 0
        self.gate = asyncio.Event()
        self._body = body
        self._exc = exc
        self._status = status

    @override
    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        self.calls += 1
        await self.gate.wait()
        if self._exc is not None:
            raise self._exc
        return httpx2.Response(self._status, content=self._body, request=request)


def _cache_no_get() -> FilterPolicy:
    """Cache only POSTs, so no GET in a test is stored."""
    return FilterPolicy(request_filters=[MethodFilter(frozenset({"POST"}))])


async def _get(transport: ResilientCacheTransport, url: str = _URL) -> bytes:
    """GET *url* through *transport* and read the body."""
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
    """Identical GETs in flight together make one fetch, and each gets a response object of its own."""
    network = _GatedNetwork()
    transport = cache_transport(storage, network)

    tasks = [
        asyncio.create_task(transport.handle_async_request(httpx2.Request("GET", _URL))) for _ in range(_CONCURRENT)
    ]
    await _eventually(lambda: network.calls == 1)
    await _let_waiters_arrive()
    network.gate.set()
    responses = await asyncio.gather(*tasks)

    assert network.calls == 1
    assert len({id(response) for response in responses}) == _CONCURRENT, "each request gets its own response"
    assert [await response.aread() for response in responses] == [_BODY] * _CONCURRENT
    assert {response.status_code for response in responses} == {httpx2.codes.OK}


async def test_a_get_the_policy_does_not_cache_is_shared_too_and_only_while_in_flight(
    storage: AsyncSqliteStorage,
) -> None:
    """Sharing needs no cache entry: simultaneous requests for an uncached file share the one download.

    A request after it finished fetches again, since nothing stored it and the finished download is not handed out
    twice.
    """
    network = _GatedNetwork()
    transport = cache_transport(storage, network, policy=_cache_no_get())

    tasks = [asyncio.create_task(_get(transport)) for _ in range(_CONCURRENT)]
    await _eventually(lambda: network.calls == 1)
    await _let_waiters_arrive()
    network.gate.set()

    assert await asyncio.gather(*tasks) == [_BODY] * _CONCURRENT
    assert network.calls == 1
    assert await _get(transport) == _BODY
    assert network.calls == len(("the shared download", "the later one"))


async def test_a_degraded_download_is_shared_too(storage: AsyncSqliteStorage) -> None:
    """A request that found the storage broken is redone live and held whole, so the requests waiting on it share it."""
    network = _GatedNetwork()
    faulty = FaultyStorage(storage, ValueError("corrupt cache entry"), failing="get_entries")
    transport = cache_transport(faulty, network)

    tasks = [
        asyncio.create_task(transport.handle_async_request(httpx2.Request("GET", _URL))) for _ in range(_CONCURRENT)
    ]
    await _eventually(lambda: network.calls == 1)
    await _let_waiters_arrive()
    network.gate.set()
    responses = await asyncio.gather(*tasks)

    assert network.calls == 1
    assert [await response.aread() for response in responses] == [_BODY] * _CONCURRENT
    assert all(CACHE_DEGRADED_EXT in response.extensions for response in responses)


@pytest.mark.parametrize(
    "urls",
    [
        pytest.param((_URL, _URL.replace("2025", "2024")), id="different-paths"),
        pytest.param(
            (
                "https://example.test/download?reportDate=20260917",
                "https://example.test/download?reportDate=20260918",
            ),
            id="different-queries",
        ),
    ],
)
async def test_requests_for_different_urls_do_not_share(urls: tuple[str, str], storage: AsyncSqliteStorage) -> None:
    """Two URLs are two requests, including one path with a different query each day."""
    network = _GatedNetwork()
    transport = cache_transport(storage, network)

    tasks = [asyncio.create_task(_get(transport, url)) for url in urls]
    await _eventually(lambda: network.calls == len(urls))
    network.gate.set()

    assert await asyncio.gather(*tasks) == [_BODY] * len(urls)
    assert network.calls == len(urls)


async def test_posts_are_never_shared(storage: AsyncSqliteStorage) -> None:
    """A POST's body is part of what it asks for, so two identical-looking POSTs are two requests."""
    network = _GatedNetwork()
    transport = cache_transport(storage, network)
    posts = 2

    async def post() -> bytes:
        response = await transport.handle_async_request(httpx2.Request("POST", _URL, content=b"{}"))
        return await response.aread()

    tasks = [asyncio.create_task(post()) for _ in range(posts)]
    await _eventually(lambda: network.calls == posts)
    network.gate.set()

    assert await asyncio.gather(*tasks) == [_BODY] * posts
    assert network.calls == posts


async def test_a_failed_download_fails_every_waiting_request_from_one_attempt(storage: AsyncSqliteStorage) -> None:
    """A network failure fails every waiting request; each caller's retry layer, above this one, then retries."""
    network = _GatedNetwork(exc=httpx2.ConnectError("dns failure"))
    transport = cache_transport(storage, network)

    tasks = [asyncio.create_task(_get(transport)) for _ in range(_CONCURRENT)]
    await _eventually(lambda: network.calls == 1)
    await _let_waiters_arrive()
    network.gate.set()
    results = await asyncio.gather(*tasks, return_exceptions=True)

    assert all(isinstance(result, httpx2.ConnectError) for result in results), results
    assert network.calls == 1


async def test_a_cancelled_first_request_hands_the_download_to_the_next(storage: AsyncSqliteStorage) -> None:
    """When the request fetching for the others is cancelled, the next waiting one fetches for the rest."""
    network = _GatedNetwork()
    transport = cache_transport(storage, network)

    first = asyncio.create_task(_get(transport))
    await _eventually(lambda: network.calls == 1)
    waiting = [asyncio.create_task(_get(transport)) for _ in range(_WAITING)]
    await _let_waiters_arrive()
    first.cancel()
    await _eventually(lambda: network.calls == len(("the cancelled download", "the handed-over one")))
    network.gate.set()

    assert await asyncio.gather(*waiting) == [_BODY] * _WAITING
    with pytest.raises(asyncio.CancelledError):
        await first
    assert network.calls == len(("the cancelled download", "the one the waiting requests shared"))


async def test_a_cancelled_waiting_request_does_not_cancel_the_download(storage: AsyncSqliteStorage) -> None:
    """A waiting request that is cancelled leaves the download, and every other request waiting on it, untouched."""
    network = _GatedNetwork()
    transport = cache_transport(storage, network)

    first = asyncio.create_task(_get(transport))
    await _eventually(lambda: network.calls == 1)
    waiting = [asyncio.create_task(_get(transport)) for _ in range(_WAITING)]
    await _let_waiters_arrive()
    waiting[0].cancel()
    await _let_waiters_arrive()
    network.gate.set()

    assert await first == _BODY
    assert await asyncio.gather(*waiting[1:]) == [_BODY] * (_WAITING - 1)
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
            if self._later == len(("one later fetch", "another")):
                self._together.set()
            await self._together.wait()
        return httpx2.Response(httpx2.codes.OK, content=self._body, request=request)


async def test_a_response_too_large_to_hold_is_not_shared(storage: AsyncSqliteStorage) -> None:
    """Past the cap the body is not held, so there is nothing to share.

    The requests that waited each fetch their own, at the same time, and each client then raises its own too-large
    error, as it would without sharing.
    """
    body = b"0123456789"
    network = _OverCapNetwork(body)
    transport = cache_transport(storage, network, cap=len(body) // 2)

    tasks = [asyncio.create_task(_get(transport)) for _ in range(_WAITING)]
    bodies = await asyncio.wait_for(asyncio.gather(*tasks), timeout=5)

    assert bodies == [body] * _WAITING
    assert network.calls == _WAITING


#: A body that arrives in several chunks, so a cap can fall between them.
_CHUNKS = (b"0123456789",) * 4


async def test_a_body_past_the_cap_is_replayed_whole(storage: AsyncSqliteStorage) -> None:
    """A body past the cap is handed back as the part already read followed by the unread rest."""
    network = TrackingNetwork(_CHUNKS)
    transport = cache_transport(storage, network, cap=len(_CHUNKS[0]) + 1)

    assert await _get(transport) == b"".join(_CHUNKS)


async def test_closing_a_body_past_the_cap_closes_the_network_response(storage: AsyncSqliteStorage) -> None:
    """A client that stops reading at its cap closes the response, and with it the network response under hishel's.

    hishel's own response does not close the network response it streams from, so a replay that closed only hishel's
    would keep the pooled connection checked out.
    """
    network = TrackingNetwork(_CHUNKS)
    transport = cache_transport(storage, network, cap=len(_CHUNKS[0]) + 1)

    response = await transport.handle_async_request(httpx2.Request("GET", _URL))
    async for _ in response.aiter_raw():
        break
    await response.aclose()

    assert network.streams[0].closed


class _Unreachable(httpx2.AsyncBaseTransport):
    """A network leg that fails every fetch with a fresh error, so no test object keeps the failure alive."""

    @override
    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        msg = "dns failure"
        raise httpx2.ConnectError(msg, request=request)


async def test_a_failure_no_request_waited_on_is_not_reported_as_never_retrieved(
    storage: AsyncSqliteStorage, caplog: pytest.LogCaptureFixture
) -> None:
    """A lone failed GET leaves nothing whose exception nobody retrieved, which asyncio would log when collected."""
    transport = cache_transport(storage, _Unreachable())

    with caplog.at_level(logging.ERROR, logger="asyncio"):
        with pytest.raises(httpx2.ConnectError):
            await _get(transport)
        gc.collect()

    assert "never retrieved" not in caplog.text


async def test_gets_for_one_url_with_different_headers_do_not_share(storage: AsyncSqliteStorage) -> None:
    """A ``Range``, an ``Accept`` or a credential changes what a GET asks for, so it gets its own download."""
    network = _GatedNetwork()
    transport = cache_transport(storage, network, policy=_cache_no_get())

    requests = [httpx2.Request("GET", _URL, headers={"Range": f"bytes={start}-"}) for start in (0, 100)]
    tasks = [asyncio.create_task(transport.handle_async_request(request)) for request in requests]
    await _eventually(lambda: network.calls == len(requests))
    network.gate.set()
    await asyncio.gather(*tasks)

    assert network.calls == len(requests)


async def _gather_gated(network: _GatedNetwork, tasks: list[asyncio.Task[bytes]]) -> list[bytes | BaseException]:
    """Release *network* once the first of *tasks* is fetching and the rest wait on it, then gather them all."""
    await _eventually(lambda: network.calls == 1)
    await _let_waiters_arrive()
    network.gate.set()
    return await asyncio.gather(*tasks, return_exceptions=True)


async def test_each_waiting_request_raises_its_own_copy_of_a_shared_failure(storage: AsyncSqliteStorage) -> None:
    """A failure re-raised by every waiting task would grow one traceback by each; each raises its own copy instead.

    The copy keeps the type, chains to the original, has a notes list of its own, and is marked as not this request's
    own attempt; the request that fetched raises the original, unmarked.
    """
    original = httpx2.ConnectError("dns failure")
    original.add_note("resolver: 10.0.0.1")
    network = _GatedNetwork(exc=original)
    transport = cache_transport(storage, network)

    results = await _gather_gated(network, [asyncio.create_task(_get(transport)) for _ in range(_CONCURRENT)])

    first, *copies = results
    assert first is original
    assert not host_untested(original)
    assert len({id(copy) for copy in copies}) == _CONCURRENT - 1
    for copy in copies:
        assert isinstance(copy, httpx2.ConnectError)
        assert copy.__cause__ is original
        assert str(copy) == str(original)
        assert copy.__notes__ == original.__notes__
        assert copy.__notes__ is not original.__notes__
        assert host_untested(copy)


async def test_an_oserror_is_shared_like_any_network_failure(storage: AsyncSqliteStorage) -> None:
    """A custom transport's ``OSError`` is a network failure to the retry layer, so the waiting requests share it."""
    network = _GatedNetwork(exc=ConnectionRefusedError("refused"))
    transport = cache_transport(storage, network)

    results = await _gather_gated(network, [asyncio.create_task(_get(transport)) for _ in range(_CONCURRENT)])

    assert all(isinstance(result, ConnectionRefusedError) for result in results), results
    assert network.calls == 1


async def test_a_shared_response_is_marked_and_the_fetched_one_is_not(storage: AsyncSqliteStorage) -> None:
    """The request that fetched holds evidence about the host; a copy handed to a waiting request holds none."""
    network = _GatedNetwork()
    transport = cache_transport(storage, network)

    tasks = [
        asyncio.create_task(transport.handle_async_request(httpx2.Request("GET", _URL))) for _ in range(_CONCURRENT)
    ]
    await _eventually(lambda: network.calls == 1)
    await _let_waiters_arrive()
    network.gate.set()
    first, *shared = await asyncio.gather(*tasks)

    assert not host_untested(first)
    assert all(host_untested(response) for response in shared)


def _timed(read: float) -> httpx2.Request:
    """Return a GET of the shared URL whose read timeout is *read*."""
    return httpx2.Request("GET", _URL, extensions={"timeout": {"read": read}})


def _retried(policy: RetryPolicy) -> httpx2.Request:
    """Return a GET of the shared URL whose call carries its own retry *policy*."""
    request = httpx2.Request("GET", _URL)
    context = CallContext(CallInfo(method="GET", url=_URL))
    context.put(RETRY_POLICY, policy)
    context.attach(request)
    return request


@pytest.mark.parametrize(
    "build",
    [
        pytest.param(lambda: (_timed(1.0), _timed(60.0)), id="timeouts"),
        pytest.param(lambda: (_retried(NO_RETRY), _retried(RetryPolicy(attempts=5))), id="retry-policies"),
    ],
)
async def test_gets_that_differ_in_timeout_or_retry_policy_do_not_share(
    build: Callable[[], tuple[httpx2.Request, httpx2.Request]], storage: AsyncSqliteStorage
) -> None:
    """A GET willing to wait longer, or to try more often, must not inherit the failure of one that was not."""
    network = _GatedNetwork()
    transport = cache_transport(storage, network, policy=_cache_no_get())

    requests = build()
    tasks = [asyncio.create_task(transport.handle_async_request(request)) for request in requests]
    await _eventually(lambda: network.calls == len(requests))
    network.gate.set()
    await asyncio.gather(*tasks)

    assert network.calls == len(requests)


def _sharing_client(network: _GatedNetwork, storage: AsyncSqliteStorage) -> StubClient:
    """Build a client over *network* that caches every GET and sends each request once."""
    return StubClient(
        base_url="https://example.test",
        rate=UNLIMITED,
        policy=TransportPolicy(
            retry=RetryPolicy(attempts=1), cache=HttpCache(policy=every_get_policy(), storage=storage)
        ),
        http=HttpOptions(transport=network),
    )


@pytest.mark.parametrize(
    ("make_network", "raised"),
    [
        pytest.param(lambda: _GatedNetwork(status=httpx2.codes.SERVICE_UNAVAILABLE), UpstreamStatusError, id="a-503"),
        pytest.param(lambda: _GatedNetwork(exc=httpx2.ConnectError("refused")), TransportError, id="a-connect-error"),
    ],
)
async def test_one_shared_upstream_failure_is_one_breaker_failure(
    make_network: Callable[[], _GatedNetwork], raised: type[Exception], storage: AsyncSqliteStorage
) -> None:
    """Five identical GETs that shared one failed fetch are one failure of the host, not five, so none trips it.

    Only the request that fetched counts; the waiting ones received its outcome and record nothing.
    """
    network = make_network()
    client = _sharing_client(network, storage)
    try:
        tasks = [asyncio.create_task(client.get("files/2025.zip", expect=as_bytes)) for _ in range(_CONCURRENT)]
        results = await _gather_gated(network, tasks)
    finally:
        await client.aclose()

    assert all(isinstance(result, raised) for result in results), results
    assert network.calls == 1
    assert failures("example.test") == 1


async def test_gets_whose_timeouts_the_deadline_cut_still_share(storage: AsyncSqliteStorage) -> None:
    """GETs whose read timeout outlasts the deadline share one download, though each one's fitted timeout differs.

    The deadline cuts every request's timeouts to the time it has left, which is different on every request; the
    sharing key uses the timeouts the call asked for.
    """
    network = _GatedNetwork()
    client = StubClient(
        base_url="https://example.test",
        rate=UNLIMITED,
        policy=TransportPolicy(
            deadline=20.0, retry=RetryPolicy(attempts=1), cache=HttpCache(policy=every_get_policy(), storage=storage)
        ),
        http=HttpOptions(transport=network),
    )
    try:
        tasks = [asyncio.create_task(client.get("files/2025.zip", expect=as_bytes)) for _ in range(_CONCURRENT)]
        results = await _gather_gated(network, tasks)
    finally:
        await client.aclose()

    assert results == [_BODY] * _CONCURRENT
    assert network.calls == 1


def _due_in(seconds: float) -> httpx2.Request:
    """Return a GET of the shared URL asking for a 60 s read timeout, fitted to a deadline *seconds* away."""
    request = httpx2.Request("GET", _URL, extensions={"timeout": {"read": 60.0}})
    context = CallContext(CallInfo(method="GET", url=_URL), deadline_after=seconds)
    context.attach(request)
    context.fit_timeouts(request)
    return request


@pytest.mark.parametrize(
    ("first_due_in", "calls"),
    [
        pytest.param(5.0, len(("the first request's", "the waiting one's own")), id="cut-by-the-deadline"),
        pytest.param(120.0, 1, id="the-timeout-it-asked-for"),
    ],
)
async def test_a_timeout_the_deadline_cut_short_is_not_shared(
    first_due_in: float, calls: int, storage: AsyncSqliteStorage
) -> None:
    """A waiting GET with more time left fetches for itself when the first one timed out on its own deadline.

    A timeout of the read timeout both asked for is shared like any other network failure.
    """
    network = _GatedNetwork(exc=httpx2.ReadTimeout("slow"))
    transport = cache_transport(storage, network, policy=_cache_no_get())

    first = asyncio.create_task(transport.handle_async_request(_due_in(first_due_in)))
    await _eventually(lambda: network.calls == 1)
    waiting = asyncio.create_task(transport.handle_async_request(_due_in(120.0)))
    await _let_waiters_arrive()
    network.gate.set()
    results = await asyncio.gather(first, waiting, return_exceptions=True)

    assert all(isinstance(result, httpx2.ReadTimeout) for result in results), results
    assert network.calls == calls
