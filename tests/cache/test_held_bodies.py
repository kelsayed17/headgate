# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""What the cache layer hands back of a body it reads, and when a body is held whole to share.

A body is read inside the layer up to the client's cap: one that fits is held whole, and identical GETs in flight with
it share it; one past the cap is handed back as a replay of what was read followed by the rest, so the client's own
cap check raises. Each is checked here at the boundary, with the response's headers and extensions intact, on the
cached path and on the degraded live path.
"""

from __future__ import annotations

import asyncio
import gzip
import os
from typing import TYPE_CHECKING, Final, override

import httpx2
import pytest

from headgate.cache import Validator
from headgate.cache._transport import CACHE_DEGRADED_EXT
from tests.cache._helpers import FaultyStorage, TrackedStream, cache_transport

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from hishel import AsyncSqliteStorage

    from headgate.cache._transport import ResilientCacheTransport

_URL: Final = "https://example.test/files/report.csv"
#: A body that arrives in several chunks.
_CHUNKS: Final = (b"0123456789", b"abcdefghij", b"KLMNOPQRST")
_BODY: Final = b"".join(_CHUNKS)
#: How many identical requests a sharing test sends at once.
_CONCURRENT: Final = 3
#: The longest degraded marker the cache layer sets.
_MARKER_LIMIT: Final = 240
#: A header the network answers with, which every copy of a response must keep.
_MARK: Final = ("x-report", "q3")


class _Gated(httpx2.AsyncBaseTransport):
    """The network leg: counts fetches, holds each until the test opens the gate, then answers with a stream."""

    def __init__(self, chunks: tuple[bytes, ...] = _CHUNKS, headers: dict[str, str] | None = None) -> None:
        """Answer with *chunks* streamed one at a time, and *headers*."""
        self.calls = 0
        self.gate = asyncio.Event()
        self._chunks = chunks
        self._headers = {_MARK[0]: _MARK[1], **(headers or {})}

    @override
    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        self.calls += 1
        await self.gate.wait()
        return httpx2.Response(httpx2.codes.OK, headers=self._headers, stream=TrackedStream(self._chunks))


class _InMemory(_Gated):
    """The network leg answering with a body already in memory, as a response built from ``content=`` is."""

    @override
    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        self.calls += 1
        await self.gate.wait()
        return httpx2.Response(httpx2.codes.OK, headers=self._headers, content=b"".join(self._chunks))


async def _together(transport: ResilientCacheTransport, network: _Gated) -> list[httpx2.Response]:
    """Send :data:`_CONCURRENT` identical GETs that are all in flight before the network answers the first."""
    tasks = [
        asyncio.create_task(transport.handle_async_request(httpx2.Request("GET", _URL))) for _ in range(_CONCURRENT)
    ]
    for _ in range(200):
        await asyncio.sleep(0.005)
        if network.calls:
            break
    for _ in range(20):
        await asyncio.sleep(0)
    network.gate.set()
    return await asyncio.wait_for(asyncio.gather(*tasks), timeout=5)


async def test_a_body_in_several_chunks_comes_back_whole(storage: AsyncSqliteStorage) -> None:
    """Hand back a body read in several chunks exactly as sent, joined with nothing between them."""
    network = _Gated()
    network.gate.set()

    response = await cache_transport(storage, network).handle_async_request(httpx2.Request("GET", _URL))

    assert await response.aread() == _BODY


@pytest.mark.parametrize("network_type", [_Gated, _InMemory], ids=["streamed", "in-memory"])
async def test_a_body_exactly_at_the_cap_is_held_and_shared(
    network_type: type[_Gated], storage: AsyncSqliteStorage
) -> None:
    """Hold a body of exactly the cap whole, so identical GETs in flight share one fetch, each with every header."""
    network = network_type()
    faulty = FaultyStorage(storage, ValueError("corrupt"), failing="get_entries" if network_type is _InMemory else None)

    responses = await _together(cache_transport(faulty, network, cap=len(_BODY)), network)

    assert network.calls == 1
    assert [await response.aread() for response in responses] == [_BODY] * _CONCURRENT
    assert [response.headers.get(_MARK[0]) for response in responses] == [_MARK[1]] * _CONCURRENT


async def test_a_degraded_body_past_the_cap_is_not_shared(storage: AsyncSqliteStorage) -> None:
    """Fetch a body past the cap once per request on the degraded path too: it is not held, so nothing is shared."""
    network = _InMemory()
    faulty = FaultyStorage(storage, ValueError("corrupt"), failing="get_entries")
    responses = await _together(cache_transport(faulty, network, cap=len(_BODY) - 1), network)

    assert network.calls == _CONCURRENT
    assert [await response.aread() for response in responses] == [_BODY] * _CONCURRENT


async def test_a_body_past_the_cap_keeps_its_encoding_so_it_decodes(storage: AsyncSqliteStorage) -> None:
    """Hand back a body past the cap with its headers, so a gzip body decodes to what fits under the cap."""
    data = os.urandom(len(_BODY))
    compressed = gzip.compress(data)
    network = _Gated(chunks=(compressed[:8], compressed[8:]), headers={"content-encoding": "gzip"})
    network.gate.set()
    transport = cache_transport(storage, network, cap=len(data))

    response = await transport.handle_async_request(httpx2.Request("GET", _URL))

    assert len(compressed) > len(data), "random bytes do not compress, so the raw body is past the cap"
    assert await response.aread() == data
    assert response.headers[_MARK[0]] == _MARK[1]


async def test_a_stored_body_past_the_cap_is_still_reported_as_from_the_cache(storage: AsyncSqliteStorage) -> None:
    """Keep the stored response's extensions on a body past the cap: a hit is a hit, whoever's cap it exceeds."""
    network = _Gated()
    network.gate.set()
    stored = await cache_transport(storage, network).handle_async_request(httpx2.Request("GET", _URL))
    await stored.aread()

    small = cache_transport(storage, network, cap=len(_BODY) // 2)
    response = await small.handle_async_request(httpx2.Request("GET", _URL))

    assert network.calls == 1
    assert response.extensions.get("hishel_from_cache") is True
    assert await response.aread() == _BODY


async def test_the_degraded_marker_drops_userinfo_and_is_bounded(storage: AsyncSqliteStorage) -> None:
    """Name the URL in the degraded marker without its userinfo, cut to 240 characters."""
    faulty = FaultyStorage(storage, ValueError("corrupt"), failing="get_entries")
    network = _InMemory()
    network.gate.set()
    long_path = "a" * _MARKER_LIMIT
    request = httpx2.Request("GET", f"https://user:pw@example.test/{long_path}")

    response = await cache_transport(faulty, network).handle_async_request(request)

    marker = response.extensions[CACHE_DEGRADED_EXT]
    assert marker == f"https://example.test/{long_path}"[:_MARKER_LIMIT]
    assert len(marker) == _MARKER_LIMIT


class _Stalls(TrackedStream):
    """A network body that sends its first chunk, then never another."""

    def __init__(self, stalled: asyncio.Event) -> None:
        """Set *stalled* once the first chunk is out."""
        super().__init__()
        self._stalled = stalled

    @override
    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield _CHUNKS[0]
        self._stalled.set()
        await asyncio.Event().wait()


async def test_a_cancelled_degraded_fetch_closes_the_network_response(storage: AsyncSqliteStorage) -> None:
    """Close the live response of a degraded fetch cancelled while its body is read, so no connection leaks."""
    stalled = asyncio.Event()
    streams: list[_Stalls] = []

    class _Stalling(httpx2.AsyncBaseTransport):
        @override
        async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
            streams.append(_Stalls(stalled))
            return httpx2.Response(httpx2.codes.OK, stream=streams[-1])

    faulty = FaultyStorage(storage, ValueError("corrupt"), failing="get_entries")
    task = asyncio.create_task(cache_transport(faulty, _Stalling()).handle_async_request(httpx2.Request("GET", _URL)))
    await stalled.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert streams[0].closed


class _CloseFails(TrackedStream):
    """A network body whose close fails, as a dropped connection's can."""

    @override
    async def aclose(self) -> None:
        await super().aclose()
        msg = "connection reset while closing"
        raise httpx2.ReadError(msg)


async def test_a_close_that_fails_while_a_body_past_the_cap_is_abandoned_is_quiet(storage: AsyncSqliteStorage) -> None:
    """Close an abandoned body past the cap quietly, though the network response's own close fails."""
    streams: list[_CloseFails] = []

    class _Network(httpx2.AsyncBaseTransport):
        @override
        async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
            streams.append(_CloseFails(_CHUNKS))
            return httpx2.Response(httpx2.codes.OK, stream=streams[-1])

    transport = cache_transport(storage, _Network(), cap=len(_CHUNKS[0]) + 1)
    response = await transport.handle_async_request(httpx2.Request("GET", _URL))
    async for _ in response.aiter_raw():
        break
    await response.aclose()

    assert streams[0].closed


async def test_a_get_with_no_read_timeout_is_fetched(storage: AsyncSqliteStorage) -> None:
    """Fetch a GET whose timeouts mix a number and ``None`` (no read timeout), which the sharing key must order."""
    network = _Gated()
    network.gate.set()
    timeout = {"connect": 1.0, "read": None, "write": 1.0, "pool": 1.0}
    request = httpx2.Request("GET", _URL, extensions={"timeout": timeout})

    response = await cache_transport(storage, network).handle_async_request(request)

    assert await response.aread() == _BODY


# Validator.of


def test_a_validator_keeps_the_held_part_a_response_does_not_carry() -> None:
    """Take each part a response lacks from the validator held, and each part it carries from the response."""
    held = Validator(etag='"v1"', last_modified="Mon, 01 Jan 2024 00:00:00 GMT")

    dated = Validator.of(httpx2.Response(200, headers={"Last-Modified": "Tue, 02 Jan 2024 00:00:00 GMT"}), held=held)
    tagged = Validator.of(httpx2.Response(200, headers={"ETag": '"v2"'}), held=held)

    assert dated == Validator(etag='"v1"', last_modified="Tue, 02 Jan 2024 00:00:00 GMT")
    assert tagged == Validator(etag='"v2"', last_modified="Mon, 01 Jan 2024 00:00:00 GMT")
