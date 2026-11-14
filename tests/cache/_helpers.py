# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""Cache rules, storages and network legs shared by the cache suite."""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, TypedDict, override

import httpx2
from hishel import AsyncBaseStorage, AsyncSqliteStorage, Entry, Request, Response

from headgate import HttpOptions, TransportPolicy
from headgate.cache import HttpCache
from headgate.cache._transport import ResilientCacheTransport, store_accepted
from tests.support.clients import StubClient

if TYPE_CHECKING:
    import re
    import uuid
    from collections.abc import AsyncIterator, Iterable, Mapping

    from hishel import Headers

    from headgate import Rate
    from headgate.cache._credentials import CredentialScope

#: A body cap no test body reaches.
GENEROUS_CAP = 1024 * 1024

#: The body every :class:`Network` answers with.
NETWORK_BODY = b"from-network"


class CacheRules(TypedDict, total=False):
    """What an :class:`HttpCache` keeps: its ``methods``, ``paths`` and ``statuses``, as ``HttpCache(**rules)``."""

    methods: Iterable[str]
    paths: Iterable[str | re.Pattern[str]]
    statuses: Iterable[int]


def series_rules() -> CacheRules:
    """Cache the 200s of any GET whose path contains ``/series``."""
    return {"paths": ("/series",)}


def every_get_rules() -> CacheRules:
    """Cache the 200s of every GET, so hishel reaches the storage on each one."""
    return {}


def stamped_ttl(extensions: Mapping[str, object]) -> object:
    """Return the TTL a request's *extensions* carry in hishel's ``caching`` settings, or ``None`` without one."""
    settings = extensions.get("caching")
    return settings.get("ttl") if isinstance(settings, dict) else None


def stub_client(
    *,
    transport: httpx2.AsyncBaseTransport | None = None,
    cache: HttpCache | None = None,
    rate: Rate | None = None,
    http: HttpOptions | None = None,
    policy: TransportPolicy | None = None,
) -> StubClient:
    """Build a client for ``https://x`` over *transport*, caching through *cache* when given.

    *http* carries any other HTTP setting; its ``transport`` is replaced by the one given here. *policy* carries any
    other policy setting; its ``cache`` is replaced by *cache*.
    """
    return StubClient(
        base_url="https://x",
        rate=rate,
        policy=dataclasses.replace(policy or TransportPolicy(), cache=cache),
        http=dataclasses.replace(http or HttpOptions(), transport=transport),
    )


async def accept(response: httpx2.Response) -> bytes:
    """Read *response* and accept it, as a client does once the call's reader returns: a miss is stored now."""
    body = await response.aread()
    await store_accepted(response)
    return body


def cache_transport(
    storage: AsyncBaseStorage,
    network: httpx2.AsyncBaseTransport,
    *,
    rules: CacheRules | None = None,
    cap: int = GENEROUS_CAP,
    scope: CredentialScope | None = None,
) -> ResilientCacheTransport:
    """Build a cache transport over *network*, caching every GET's 200 into *storage* unless *rules* say otherwise.

    *scope* views requests as a client with that auth would; without one, as a client with no auth.
    """
    cache = HttpCache(**(rules or every_get_rules()), storage=storage)
    return ResilientCacheTransport(network, cache, max_body_bytes=cap, scope=scope)


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

    def __init__(self, chunks: tuple[bytes, ...] = (NETWORK_BODY,), *, status: int = httpx2.codes.OK) -> None:
        """Answer every request with *status* and a fresh :class:`TrackedStream` of *chunks*."""
        self.streams: list[TrackedStream] = []
        self.urls: list[str] = []
        self._chunks = chunks
        self._status = status

    @override
    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        self.urls.append(str(request.url))
        stream = TrackedStream(self._chunks)
        self.streams.append(stream)
        return httpx2.Response(self._status, stream=stream)


class FaultyStorage(AsyncBaseStorage):
    """Delegate to a real storage, but raise *exc* from the one method named by *failing* (or none, when ``None``).

    A fault on exactly one operation while every other one behaves normally.
    """

    def __init__(self, inner: AsyncBaseStorage, exc: Exception | None = None, *, failing: str | None = None) -> None:
        """Delegate to *inner*, raising *exc* from the method named *failing*."""
        super().__init__()
        self.inner = inner
        self.exc = exc
        self._failing = failing

    def _maybe_fail(self, method: str) -> None:
        if method == self._failing and self.exc is not None:
            raise self.exc

    @override
    async def create_entry(
        self, request: Request, response: Response, key: str, *, ttl: float | None = None, tti: float | None = None
    ) -> Entry:
        self._maybe_fail("create_entry")
        return await self.inner.create_entry(request, response, key, ttl=ttl, tti=tti)

    @override
    async def get_entries(self, key: str) -> list[Entry]:
        self._maybe_fail("get_entries")
        return await self.inner.get_entries(key)

    @override
    async def update_headers(self, id: uuid.UUID, headers: Headers) -> None:
        self._maybe_fail("update_headers")
        await self.inner.update_headers(id, headers)

    @override
    async def remove_entry(self, id: uuid.UUID) -> None:
        self._maybe_fail("remove_entry")
        await self.inner.remove_entry(id)

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
