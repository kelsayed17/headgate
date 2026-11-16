# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""A stream's deadline bounds opening it; once the head has arrived, only the read timeout bounds each read.

These run against a loopback server on the real clock, since what is measured is httpx2's own read timeout.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import TYPE_CHECKING

import pytest

from headgate import Client, ClientTimeoutError, Timeouts, TransportPolicy

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Awaitable, Callable

#: The call's deadline: shorter than every gap between the chunks below.
_DEADLINE = 0.2
#: The gap between chunks, longer than the deadline and shorter than the read timeout.
_GAP = 0.4
_CHUNKS = 3
_HEAD = b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nTransfer-Encoding: chunked\r\n\r\n"

type _Handler = Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]]


@contextlib.asynccontextmanager
async def _server(handler: _Handler) -> AsyncGenerator[str]:
    """Serve *handler* on a loopback port, yielding its base URL."""

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readuntil(b"\r\n\r\n")
        with contextlib.suppress(ConnectionError):
            await handler(reader, writer)
        writer.close()

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    host, port = server.sockets[0].getsockname()[:2]
    try:
        yield f"http://{host}:{port}"
    finally:
        server.close()
        await server.wait_closed()


async def _trickle(_reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """Send the head at once, then one chunk every :data:`_GAP` seconds, then the end of the body."""
    writer.write(_HEAD)
    await writer.drain()
    for number in range(_CHUNKS):
        await asyncio.sleep(_GAP)
        writer.write(b"2\r\n%d\n\r\n" % number)
        await writer.drain()
    writer.write(b"0\r\n\r\n")
    await writer.drain()


async def _silent(reader: asyncio.StreamReader, _writer: asyncio.StreamWriter) -> None:
    """Never answer: wait until the client hangs up."""
    await reader.read()


async def test_a_stream_outlives_its_deadline_while_chunks_keep_coming() -> None:
    """Read a body whose chunks come further apart than the whole deadline, each within the default read timeout."""
    policy = TransportPolicy(retry=None, deadline=_DEADLINE)
    async with _server(_trickle) as url, Client(base_url=url, policy=policy) as client:
        started = time.monotonic()
        async with client.stream("feed") as stream:
            body = b"".join([chunk async for chunk in stream.aiter_bytes()])

    assert body == b"0\n1\n2\n"
    assert time.monotonic() - started > _DEADLINE * _CHUNKS, "the stream ran past its deadline"


async def test_a_streams_read_timeout_may_be_longer_than_its_deadline() -> None:
    """Accept a call's read timeout longer than its deadline on a stream, where the deadline only bounds opening."""
    async with (
        _server(_trickle) as url,
        Client(base_url=url, policy=TransportPolicy(retry=None)) as client,
        client.stream("feed", deadline=_DEADLINE, timeout=Timeouts(read=_GAP * 5)) as stream,
    ):
        body = b"".join([chunk async for chunk in stream.aiter_bytes()])

    assert body == b"0\n1\n2\n"


async def test_a_stream_whose_head_never_comes_fails_at_its_deadline() -> None:
    """Fail at the deadline when no response head arrives, however long the read timeout is."""
    async with _server(_silent) as url, Client(base_url=url, policy=TransportPolicy(retry=None)) as client:
        started = time.monotonic()
        with pytest.raises(ClientTimeoutError):
            async with client.stream("feed", deadline=_DEADLINE, timeout=Timeouts(read=60.0)):
                pass

    assert time.monotonic() - started < _GAP * 5, "the deadline, not the read timeout, ended it"
