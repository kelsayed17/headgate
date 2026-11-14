# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""What a cached client sends straight through, and what it still bounds, labels and checks.

A cache holds a body whole only where that buys something: a GET its rules name, read by a verb. A stream, a request
the rules do not name and a POST go to the network as they arrive, never read ahead and never shared. A second send of
one call (a hedge) fetches for itself. The deferred write of a miss ends with the call's deadline. A raw UTF-8 header
value passes through, a tracer's headers do not keep identical GETs apart, a guarded client never hits what an
unguarded one fetched, and a per-call ``cache_ttl`` is checked before anything is sent.
"""

from __future__ import annotations

import asyncio
import math
from typing import TYPE_CHECKING, Final, override

import httpx2
import pytest

from headgate import HttpCache, HttpOptions, Method, SecurityOptions, TransportPolicy, as_bytes
from headgate._core import CallContext, CallInfo
from headgate.cache import Partition
from headgate.cache._transport import CACHE_DEGRADATIONS
from headgate.errors import ForbiddenDestinationError
from headgate.ratelimit import UNLIMITED
from tests.cache._helpers import NETWORK_BODY, FaultyStorage, Network, cache_transport, series_rules, stub_client
from tests.observe.test_otel import Telemetry
from tests.support.clients import StubClient
from tests.support.responses import RESPONSE

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence

    from hishel import AsyncSqliteStorage, Entry, Request, Response

_URL: Final = "https://x/series"
_FIRST: Final = b"data: one\n\n"
_REST: Final = b"data: two\n\n"
#: How many requests a test sends of one thing.
_PAIR: Final = 2


class _Trickle(httpx2.AsyncByteStream):
    """A body whose first chunk arrives at once and whose rest waits for *gate*, as an event stream's does."""

    def __init__(self, gate: asyncio.Event) -> None:
        self._gate = gate

    @override
    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield _FIRST
        await self._gate.wait()
        yield _REST


class _Events(httpx2.AsyncBaseTransport):
    """The network leg: counts sends, and answers each with a :class:`_Trickle` the test releases."""

    def __init__(self) -> None:
        self.sent = 0
        self.gate = asyncio.Event()

    @override
    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        self.sent += 1
        return httpx2.Response(httpx2.codes.OK, stream=_Trickle(self.gate))


# H6: a stream, a POST and a request the rules do not name are never read ahead.


@pytest.mark.parametrize("failing", [None, "get_entries"], ids=["cached", "degraded"])
@pytest.mark.parametrize("method", ["GET", "POST"])
async def test_streams_open_before_their_bodies_end_each_on_its_own_download(
    method: Method, failing: str | None, storage: AsyncSqliteStorage
) -> None:
    """Open two identical streams on a cached client while their bodies are still arriving, one send each.

    Also when the storage fails the lookup, so the stream is redone live.
    """
    network = _Events()
    faulty = FaultyStorage(storage, ValueError("corrupt"), failing=failing)
    client = stub_client(transport=network, cache=HttpCache(storage=faulty))
    async with (
        client,
        asyncio.timeout(5),
        client.stream("series", method=method) as one,
        client.stream("series", method=method) as two,
    ):
        bodies = [aiter(one.aiter_bytes()), aiter(two.aiter_bytes())]
        firsts = [await anext(body) for body in bodies]
        network.gate.set()
        rests = [b"".join([chunk async for chunk in body]) for body in bodies]

    assert firsts == [_FIRST, _FIRST]
    assert rests == [_REST, _REST]
    assert network.sent == _PAIR


@pytest.mark.parametrize(("method", "url"), [("POST", _URL), ("GET", "https://x/other")], ids=["post", "unnamed-path"])
async def test_a_request_the_cache_does_not_name_is_handed_up_unread(
    method: str, url: str, storage: AsyncSqliteStorage
) -> None:
    """Hand up a POST, or a GET of a path the rules leave out, before its body has arrived."""
    network = _Events()
    transport = cache_transport(storage, network, rules=series_rules())

    response = await asyncio.wait_for(transport.handle_async_request(httpx2.Request(method, url)), timeout=5)
    network.gate.set()

    assert await response.aread() == _FIRST + _REST


# M10: a hedge never waits on its own call's first send.


class _FirstHangs(httpx2.AsyncBaseTransport):
    """The network leg: the first send never answers, every later one answers at once."""

    def __init__(self) -> None:
        self.sent = 0
        self.arrived = asyncio.Event()

    @override
    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        self.sent += 1
        self.arrived.set()
        if self.sent == 1:
            await asyncio.Event().wait()
        return httpx2.Response(httpx2.codes.OK, content=NETWORK_BODY)


async def test_a_second_send_of_one_call_fetches_for_itself(storage: AsyncSqliteStorage) -> None:
    """Answer a hedge from its own send, though its call's first send of the same GET is still in flight."""
    network = _FirstHangs()
    transport = cache_transport(storage, network)
    context = CallContext(CallInfo(method="GET", url=_URL))
    first, hedge = httpx2.Request("GET", _URL), httpx2.Request("GET", _URL)
    context.attach(first)
    context.attach(hedge)

    stuck = asyncio.create_task(transport.handle_async_request(first))
    await network.arrived.wait()
    try:
        response = await asyncio.wait_for(transport.handle_async_request(hedge), timeout=5)
    finally:
        stuck.cancel()

    assert await response.aread() == NETWORK_BODY
    assert network.sent == _PAIR


# M17: the deferred write of a miss ends with the call's deadline.


class _HungWrites(FaultyStorage):
    """A storage whose writes never finish."""

    @override
    async def create_entry(
        self, request: Request, response: Response, key: str, *, ttl: float | None = None, tti: float | None = None
    ) -> Entry:
        await asyncio.Event().wait()
        raise AssertionError  # pragma: no cover - never reached


async def test_a_hung_write_ends_with_the_deadline_and_counts_as_degraded(storage: AsyncSqliteStorage) -> None:
    """Return a verb's answer by its deadline though the write of its miss hangs, and count the write as degraded."""
    client = stub_client(transport=Network(), cache=HttpCache(storage=_HungWrites(storage)))
    before = CACHE_DEGRADATIONS["x"]
    async with client, asyncio.timeout(5):
        body = await client.get("series", expect=as_bytes, deadline=0.2)

    assert body == NETWORK_BODY
    assert CACHE_DEGRADATIONS["x"] == before + 1


# M20: a raw UTF-8 header value passes through.

_DISPOSITION: Final = 'attachment; filename="café.csv"'.encode()


class _Report(httpx2.AsyncBaseTransport):
    """The network leg: answers with a raw UTF-8 ``Content-Disposition`` and a ``Set-Cookie``."""

    @override
    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        headers = [(b"content-disposition", _DISPOSITION), (b"set-cookie", b"session=1")]
        return httpx2.Response(httpx2.codes.OK, headers=headers, content=b"a,b")


@pytest.mark.parametrize("cached", [False, True], ids=["uncached", "cached"])
async def test_a_raw_utf8_header_value_is_handed_up_as_sent(storage: AsyncSqliteStorage, *, cached: bool) -> None:
    """Read a response whose ``Content-Disposition`` holds raw UTF-8, keeping the value's bytes."""
    client = stub_client(transport=_Report(), cache=HttpCache(storage=storage) if cached else None)
    async with client:
        response = await client.get("series", expect=RESPONSE)

    assert (b"content-disposition", _DISPOSITION) in response.headers.raw
    assert response.content == b"a,b"


# M27: a tracer's headers do not keep identical GETs apart.


class _Gated(httpx2.AsyncBaseTransport):
    """The network leg: counts sends, and holds each until the test opens the gate."""

    def __init__(self) -> None:
        self.sent = 0
        self.arrived = asyncio.Event()
        self.gate = asyncio.Event()

    @override
    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        self.sent += 1
        self.arrived.set()
        await self.gate.wait()
        return httpx2.Response(httpx2.codes.OK, content=NETWORK_BODY)


async def test_traced_identical_gets_share_one_download(storage: AsyncSqliteStorage) -> None:
    """Share one download between identical GETs that OpenTelemetry gives different ``traceparent`` headers."""
    network = _Gated()
    telemetry = Telemetry()
    http = telemetry.http
    client = StubClient(
        base_url="https://x",
        rate=UNLIMITED,
        policy=TransportPolicy(cache=HttpCache(storage=storage)),
        http=HttpOptions(transport=network, observers=http.observers, middleware=http.middleware),
    )
    async with client, asyncio.timeout(5):
        calls = [asyncio.create_task(client.get("series", expect=as_bytes)) for _ in range(_PAIR)]
        await network.arrived.wait()
        for _ in range(20):
            await asyncio.sleep(0)
        network.gate.set()
        bodies = await asyncio.gather(*calls)

    assert bodies == [NETWORK_BODY, NETWORK_BODY]
    assert network.sent == 1


# M2: a guarded client never hits what an unguarded one fetched.


class _Names:
    """A resolver answering from *table*."""

    def __init__(self, **table: str) -> None:
        self._table = table

    async def __call__(self, host: str, _port: int) -> Sequence[str]:
        return (self._table[host],)


@pytest.mark.parametrize("partition", list(Partition))
async def test_a_guarded_client_is_never_served_an_unguarded_clients_entry(
    partition: Partition, storage: AsyncSqliteStorage
) -> None:
    """Refuse an internal host on a guarded client, though an unguarded one on its storage stored that host's body."""
    cache = HttpCache(storage=storage, partition=partition)
    unguarded = stub_client(transport=Network(), cache=cache)
    guarded = StubClient(
        base_url="https://x",
        rate=UNLIMITED,
        policy=TransportPolicy(cache=cache),
        http=HttpOptions(security=SecurityOptions(allow_private_hosts=False, resolver=_Names(x="10.0.0.5"))),
    )
    async with unguarded, guarded:
        assert await unguarded.get("series", expect=as_bytes) == NETWORK_BODY
        with pytest.raises(ForbiddenDestinationError):
            await guarded.get("series", expect=as_bytes)


# L31: a per-call cache_ttl is checked before anything is sent.


@pytest.mark.parametrize(
    ("ttl", "error"),
    [(math.nan, ValueError), (math.inf, ValueError), (0, ValueError), (-1.0, ValueError), ("60", TypeError)],
)
async def test_a_per_call_cache_ttl_no_entry_could_live_by_is_refused(
    ttl: float, error: type[Exception], storage: AsyncSqliteStorage
) -> None:
    """Refuse a ``cache_ttl`` that is not a positive finite number of seconds, before sending."""
    network = Network()
    client = stub_client(transport=network, cache=HttpCache(storage=storage))
    async with client:
        with pytest.raises(error, match="cache_ttl"):
            await client.get("series", expect=as_bytes, cache_ttl=ttl)

    assert network.calls == 0
