# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""``SendTrackingTransport``: each attempt's connection and written bytes, recorded as facts.

The tests that matter most run httpx2's own network transport against a loopback server, so they pin what httpcore2's
trace actually reports rather than what a fake claims it would.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING, override

import httpx2
import pytest

from headgate import IdempotencyKeys, TransportPolicy, nothing
from headgate._core import CallContext, CallInfo
from headgate.errors import TransportError
from headgate.ratelimit import Rate
from headgate.retry import SendTrackingTransport
from headgate.retry._tracking import head_size
from tests.support.clients import StubClient

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Iterator

#: The body the tests POST.
_BODY = b'{"qty": 1}'

#: A response a loopback server sends.
_OK = b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok"

type _Handler = Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]]


async def _read_head(reader: asyncio.StreamReader) -> bytes:
    """Read a request head, up to its blank line."""
    return await reader.readuntil(b"\r\n\r\n")


@contextlib.asynccontextmanager
async def _server(handler: _Handler) -> AsyncGenerator[str]:
    """Serve *handler* on a loopback port for the block, yielding its base URL."""

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await handler(reader, writer)
        finally:
            writer.close()
            with contextlib.suppress(ConnectionError):
                await writer.wait_closed()

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    host, port = server.sockets[0].getsockname()[:2]
    try:
        yield f"http://{host}:{port}"
    finally:
        server.close()
        await server.wait_closed()


async def _drop_after_head(reader: asyncio.StreamReader, _writer: asyncio.StreamWriter) -> None:
    """Read the request head, then hang up without answering."""
    await _read_head(reader)


async def _answer(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """Read the request head and body, then answer 200."""
    head = await _read_head(reader)
    length = next(
        (int(line.split(b":")[1]) for line in head.split(b"\r\n") if line.lower().startswith(b"content-length")), 0
    )
    await reader.readexactly(length)
    writer.write(_OK)
    await writer.drain()


def _tracked(request: httpx2.Request) -> CallContext:
    """Stamp a fresh context on *request*, its first attempt begun, and return it."""
    context = CallContext(CallInfo(method=request.method, url=str(request.url)))
    context.attach(request)
    context.begin_attempt()
    return context


@contextlib.asynccontextmanager
async def _network() -> AsyncGenerator[SendTrackingTransport]:
    """Yield httpx2's own network transport under the tracking layer, closed afterwards."""
    transport = SendTrackingTransport(transport=httpx2.AsyncHTTPTransport(trust_env=False))
    try:
        yield transport
    finally:
        await transport.aclose()


async def test_a_request_dropped_after_its_head_was_written_counts_the_head_and_body() -> None:
    """Record a connection and every byte written when the server hangs up without answering."""
    async with _server(_drop_after_head) as url, _network() as transport:
        request = httpx2.Request("POST", f"{url}/orders", content=_BODY)
        context = _tracked(request)
        with pytest.raises(httpx2.RemoteProtocolError):
            await transport.handle_async_request(request)

    assert context.progress.connected is True
    assert context.progress.bytes_sent == head_size(request) + len(_BODY)


async def test_a_refused_connection_records_nothing_sent() -> None:
    """Record no connection and no bytes when the connection is refused."""
    async with _server(_drop_after_head) as url:
        pass
    async with _network() as transport:
        request = httpx2.Request("POST", f"{url}/orders", content=_BODY)
        context = _tracked(request)
        with pytest.raises(httpx2.ConnectError):
            await transport.handle_async_request(request)

    assert (context.progress.connected, context.progress.bytes_sent) == (False, 0)


async def test_a_streamed_body_is_counted_chunk_by_chunk() -> None:
    """Count a streaming body's chunks as the transport takes them."""

    async def chunks() -> AsyncIterator[bytes]:
        yield b"ab"
        yield b"cde"

    async with _server(_answer) as url, _network() as transport:
        request = httpx2.Request("POST", f"{url}/upload", content=chunks())
        context = _tracked(request)
        response = await transport.handle_async_request(request)
        await response.aread()
        await response.aclose()

    assert context.progress.bytes_sent >= len(b"abcde")
    assert context.progress.connected is True


async def test_the_callers_trace_still_runs_and_is_restored() -> None:
    """Chain to a ``trace`` extension the caller set, and put it back afterwards."""
    seen: list[str] = []

    async def trace(name: str, _info: dict[str, object]) -> None:
        seen.append(name)

    async with _server(_answer) as url, _network() as transport:
        request = httpx2.Request("GET", f"{url}/x", extensions={"trace": trace})
        _tracked(request)
        response = await transport.handle_async_request(request)
        await response.aread()
        await response.aclose()

    assert "http11.send_request_headers.started" in seen
    assert request.extensions["trace"] is trace


async def test_the_tracking_extension_is_removed_afterwards() -> None:
    """Leave no ``trace`` extension behind on a request that had none."""
    async with _server(_answer) as url, _network() as transport:
        request = httpx2.Request("GET", f"{url}/x")
        _tracked(request)
        response = await transport.handle_async_request(request)
        await response.aread()
        await response.aclose()

    assert "trace" not in request.extensions


async def test_a_request_without_a_context_passes_through_untouched() -> None:
    """Send a request no client call built as it is, recording nothing."""
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200)

    request = httpx2.Request("GET", "https://api.test/x")
    await SendTrackingTransport(transport=httpx2.MockTransport(handler)).handle_async_request(request)

    assert "trace" not in seen[0].extensions
    assert CallContext.find(request) is None


class _SyncOnly(httpx2.SyncByteStream):
    """A body stream that can only be iterated synchronously."""

    @override
    def __iter__(self) -> Iterator[bytes]:
        yield b"x"


class _Inspect(httpx2.AsyncBaseTransport):
    """A transport that records the stream it was handed and answers 200."""

    def __init__(self) -> None:
        """Start with nothing seen."""
        self.streams: list[object] = []

    @override
    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        self.streams.append(request.stream)
        return httpx2.Response(200)


async def test_a_synchronous_body_stream_is_not_wrapped() -> None:
    """Leave a body that is not an async stream as it is: an async transport cannot read it anyway."""
    stream = _SyncOnly()
    inner = _Inspect()
    request = httpx2.Request("POST", "https://api.test/x", stream=stream)
    _tracked(request)

    await SendTrackingTransport(transport=inner).handle_async_request(request)

    assert inner.streams == [stream]


async def test_a_post_whose_head_reached_the_server_is_not_resent_end_to_end() -> None:
    """Send a POST once through a whole client when the server hung up after reading it: it may have been processed."""
    connections = 0

    async def count_and_drop(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        nonlocal connections
        connections += 1
        await _drop_after_head(reader, writer)

    async with _server(count_and_drop) as url:
        client = StubClient(base_url=url, rate=Rate.per_second(1000))
        with pytest.raises(TransportError) as raised:
            await client.post("orders", expect=nothing, json={"qty": 1})
        await client.aclose()

    assert connections == 1
    assert raised.value.attempts == 1


async def test_a_keyed_post_whose_head_reached_the_server_is_resent_end_to_end() -> None:
    """Re-send a POST carrying an idempotency key after the same failure."""
    connections = 0

    async def count_and_drop(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        nonlocal connections
        connections += 1
        await _drop_after_head(reader, writer)

    async with _server(count_and_drop) as url:
        client = StubClient(
            base_url=url, rate=Rate.per_second(1000), policy=TransportPolicy(idempotency=IdempotencyKeys())
        )
        with pytest.raises(TransportError):
            await client.post("orders", expect=nothing, json={"qty": 1})
        await client.aclose()

    assert connections == len(("first", "second", "third"))


class _ClosesBody(httpx2.AsyncBaseTransport):
    """A transport that closes the request's body stream, as one that gives up on a body would, and answers 200."""

    @override
    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        stream = request.stream
        if isinstance(stream, httpx2.AsyncByteStream):
            await stream.aclose()
        return httpx2.Response(200)


class _RecordsClose(httpx2.AsyncByteStream):
    """A body stream that records whether it was closed."""

    def __init__(self) -> None:
        """Start unclosed."""
        self.closed = False

    @override
    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield b"x"

    @override
    async def aclose(self) -> None:
        self.closed = True


async def test_closing_the_counted_body_closes_the_callers_stream() -> None:
    """Pass a close of the counting wrapper on to the stream it wraps."""
    stream = _RecordsClose()
    request = httpx2.Request("POST", "https://api.test/x", stream=stream)
    _tracked(request)

    await SendTrackingTransport(transport=_ClosesBody()).handle_async_request(request)

    assert stream.closed is True
