# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""Policies, storages and network legs shared by the cache suite."""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, override

import httpx2
from hishel import AsyncBaseStorage, AsyncSqliteStorage, Entry, FilterPolicy, Request, Response

from headgate import HttpOptions, TransportPolicy
from headgate._cache import MethodFilter, PathFragmentFilter, ResilientCacheTransport, StatusCodeFilter
from headgate.ratelimit import UNLIMITED
from tests.support.clients import StubClient

if TYPE_CHECKING:
    import uuid
    from collections.abc import AsyncIterator, Callable

    from headgate import HttpCache, Rate

#: A body cap no test body reaches.
GENEROUS_CAP = 1024 * 1024

#: The body every :class:`Network` answers with.
NETWORK_BODY = b"from-network"


def series_policy() -> FilterPolicy:
    """Cache the 200s of any path containing ``/series``."""
    return FilterPolicy(
        request_filters=[PathFragmentFilter(("/series",))],
        response_filters=[StatusCodeFilter(frozenset({httpx2.codes.OK}))],
    )


def every_get_policy() -> FilterPolicy:
    """Cache the 200s of every GET, so hishel reaches the storage on each one."""
    return FilterPolicy(
        request_filters=[MethodFilter(frozenset({"GET"}))],
        response_filters=[StatusCodeFilter(frozenset({httpx2.codes.OK}))],
    )


def stub_client(
    *,
    transport: httpx2.AsyncBaseTransport | None = None,
    cache: HttpCache | None = None,
    rate: Rate = UNLIMITED,
    http: HttpOptions | None = None,
) -> StubClient:
    """Build a client for ``https://x`` over *transport*, caching through *cache* when given.

    *http* carries any other HTTP setting; its ``transport`` is replaced by the one given here.
    """
    return StubClient(
        base_url="https://x",
        rate=rate,
        policy=TransportPolicy(cache=cache),
        http=dataclasses.replace(http or HttpOptions(), transport=transport),
    )


def cache_transport(
    storage: AsyncBaseStorage,
    network: httpx2.AsyncBaseTransport,
    *,
    policy: FilterPolicy | None = None,
    cap: int = GENEROUS_CAP,
) -> ResilientCacheTransport:
    """Build a cache transport over *network*, caching every GET's 200 into *storage* unless *policy* says otherwise."""
    return ResilientCacheTransport(
        next_transport=network, storage=storage, policy=policy or every_get_policy(), max_body_bytes=cap
    )


class Network(httpx2.AsyncBaseTransport):
    """The network leg: counts fetches, and raises *exc* from each one when given."""

    def __init__(self, exc: Exception | None = None) -> None:
        """Answer every request with :data:`NETWORK_BODY`, or raise *exc* when given."""
        self.calls = 0
        self._exc = exc

    @override
    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        self.calls += 1
        if self._exc is not None:
            raise self._exc
        return httpx2.Response(httpx2.codes.OK, content=NETWORK_BODY)


class TrackedStream(httpx2.AsyncByteStream):
    """A network body of *chunks* that records whether anything closed it."""

    def __init__(self, chunks: tuple[bytes, ...] = (NETWORK_BODY,)) -> None:
        """Stream *chunks* one at a time."""
        self.closed = False
        self._chunks = chunks

    @override
    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self._chunks:
            yield chunk

    @override
    async def aclose(self) -> None:
        self.closed = True


class TrackingNetwork(httpx2.AsyncBaseTransport):
    """The network leg, keeping every body it hands out and every URL it was asked for."""

    def __init__(self, chunks: tuple[bytes, ...] = (NETWORK_BODY,)) -> None:
        """Answer every request with a fresh :class:`TrackedStream` of *chunks*."""
        self.streams: list[TrackedStream] = []
        self.urls: list[str] = []
        self._chunks = chunks

    @override
    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        self.urls.append(str(request.url))
        stream = TrackedStream(self._chunks)
        self.streams.append(stream)
        return httpx2.Response(httpx2.codes.OK, stream=stream)


class FaultyStorage(AsyncBaseStorage):
    """Delegate to a real storage, but raise *exc* from the one method named by *failing* (or none, when ``None``).

    A fault on exactly one operation while every other one behaves normally.
    """

    def __init__(self, inner: AsyncBaseStorage, exc: Exception | None = None, *, failing: str | None = None) -> None:
        """Delegate to *inner*, raising *exc* from the method named *failing*."""
        self.inner = inner
        self.exc = exc
        self._failing = failing

    def _maybe_fail(self, method: str) -> None:
        if method == self._failing and self.exc is not None:
            raise self.exc

    @override
    async def create_entry(self, request: Request, response: Response, key: str, id_: uuid.UUID | None = None) -> Entry:
        self._maybe_fail("create_entry")
        return await self.inner.create_entry(request, response, key, id_)

    @override
    async def get_entries(self, key: str) -> list[Entry]:
        self._maybe_fail("get_entries")
        return await self.inner.get_entries(key)

    @override
    async def update_entry(self, id: uuid.UUID, new_entry: Entry | Callable[[Entry], Entry]) -> Entry | None:
        self._maybe_fail("update_entry")
        return await self.inner.update_entry(id, new_entry)

    @override
    async def remove_entry(self, id: uuid.UUID) -> None:
        self._maybe_fail("remove_entry")
        await self.inner.remove_entry(id)

    @override
    async def refresh_entry_ttl(self, id: uuid.UUID) -> None:
        self._maybe_fail("refresh_entry_ttl")
        await self.inner.refresh_entry_ttl(id)

    @override
    async def close(self) -> None:
        self._maybe_fail("close")
        await self.inner.close()


class CloseCountingStorage(AsyncSqliteStorage):
    """A real SQLite storage that counts how many times it was closed."""

    closes = 0

    @override
    async def close(self) -> None:
        self.closes += 1
        await super().close()
