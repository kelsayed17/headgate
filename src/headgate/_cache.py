# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""The opt-in HTTP response cache: hishel's transport, made safe to share and to fail.

`hishel <https://hishel.com>`_'s :class:`hishel.httpx2.AsyncCacheTransport` sits below retry and above the rate
limiter (see :mod:`headgate._stack`), so a cache hit short-circuits before the retry layer and spends no rate token.
``AsyncBaseClient`` marks a hit so the circuit breaker records neither a success nor a failure for it; admission is
still decided before the fetch, so a hit is refused while the breaker is open.

**The filters.** hishel ships NO concrete filters, so :class:`hishel.BaseFilter` is the intended extension point:
:class:`PathFragmentFilter`, :class:`MethodFilter` and :class:`StatusCodeFilter` compose into a per-client
:class:`hishel.FilterPolicy`. A ``FilterPolicy`` **stores UNCONDITIONALLY when a request filter matches and an allowed
status comes back, bypassing RFC-9111 ``no-store``** — which is the point when a provider sends ``no-store`` or no cache
headers at all and :class:`hishel.SpecificationPolicy` (the default) would cache nothing. Store the 200s only (a
:class:`StatusCodeFilter`) so an error body never poisons the cache. Per-client constant TTLs ride the ``hishel_ttl``
request extension, stamped by :class:`ResilientCacheTransport` from :class:`HttpCache`'s ``ttl``; body-keyed POST
caching uses hishel's native ``FilterPolicy.use_body_key``. Filters run only INSIDE hishel and see the full resolved
URL, so no other layer re-evaluates a rule: :class:`ResilientCacheTransport` stamps the client's TTL and
``AsyncBaseClient`` stamps only a per-call ``cache_ttl=``.

A storage fault never fails a request that would otherwise succeed: it is counted
(:func:`headgate.health.cache_degradations`), the request is redone uncached, and the response carries
:data:`CACHE_DEGRADED_EXT`.
"""

from __future__ import annotations

import asyncio
import contextlib
import enum
from collections import Counter
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, override

import httpx2
from hishel import AsyncBaseStorage, BaseFilter, CachePolicy, FilterPolicy, Request, Response
from hishel.httpx2 import AsyncCacheTransport

from headgate._url import is_secret_param

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping


__all__ = [
    "CACHE_DEGRADATIONS",
    "CACHE_DEGRADED_EXT",
    "HttpCache",
    "MethodFilter",
    "PathFragmentFilter",
    "ResilientCacheTransport",
    "StatusCodeFilter",
]


@dataclass(frozen=True, slots=True)
class HttpCache:
    """A client's opt-in HTTP response cache: which responses to keep, where, and for how long by default.

    ``policy`` (the allowlist) decides what is stored; ``storage`` is the shared hishel store, ``None`` when caching is
    off, in which case the client does not cache at all; ``ttl`` is the client's default lifetime, stamped on each
    request as ``hishel_ttl`` by :class:`ResilientCacheTransport` (a per-call ``cache_ttl=`` wins).
    """

    policy: FilterPolicy
    storage: AsyncBaseStorage | None
    ttl: float | None = None


# Native hishel filter primitives (composable, no I/O).
#
# Three composable :class:`hishel.BaseFilter` subclasses, the native-hishel idiom. Each is stateless with respect to the
# body (``needs_body`` is always ``False``) and decides caching from the request line (path/method) or the response
# status alone. A client composes them into its own ``FilterPolicy``.


class PathFragmentFilter(BaseFilter[Request]):
    """Cache iff any fragment appears in the request URL path."""

    def __init__(self, fragments: tuple[str, ...]) -> None:
        self._fragments = fragments

    @override
    def needs_body(self) -> bool:
        return False

    @override
    def apply(self, item: Request, body: bytes | None) -> bool:
        path = httpx2.URL(item.url).path
        return any(frag in path for frag in self._fragments)


class MethodFilter(BaseFilter[Request]):
    """Cache iff the request method is in the allowed set."""

    def __init__(self, methods: frozenset[str]) -> None:
        self._methods = methods

    @override
    def needs_body(self) -> bool:
        return False

    @override
    def apply(self, item: Request, body: bytes | None) -> bool:
        return item.method in self._methods


class StatusCodeFilter(BaseFilter[Response]):
    """Store only responses whose status is in the allowed set (never poison the cache with an error body)."""

    def __init__(self, codes: frozenset[int]) -> None:
        self._codes = codes

    @override
    def needs_body(self) -> bool:
        return False

    @override
    def apply(self, item: Response, body: bytes | None) -> bool:
        return item.status_code in self._codes


# Carrying the caller's request extensions PAST hishel.

#: The extensions of the request that entered the cache layer, for the layer below to put
#: back. A ContextVar rather than instance state because one transport serves every
#: concurrent request on a client, and each task needs its own value.
_ORIGINAL_EXTENSIONS: ContextVar[Mapping[str, Any] | None] = ContextVar(
    "headgate_cache_original_extensions", default=None
)

#: The credential query params stripped from the request before hishel saw it, for the layer below to put back.
_STRIPPED_SECRETS: ContextVar[tuple[tuple[str, str], ...]] = ContextVar("headgate_cache_stripped_secrets", default=())

#: Every network response the layer below handed up during one cache-layer call, so the fallback can close the ones
#: hishel abandoned. ``None`` outside a call.
_NETWORK_RESPONSES: ContextVar[list[httpx2.Response] | None] = ContextVar(
    "headgate_cache_network_responses", default=None
)

#: The response extension a request that found the cache broken carries: the URL (query dropped) and the error.
CACHE_DEGRADED_EXT = "headgate.cache_degraded"

#: How many requests per host found the cache broken (and were handed to the live transport), since
#: process start. Process-global like the breaker registry; read through
#: :func:`headgate.health.cache_degradations`, never written outside :meth:`_handle`.
CACHE_DEGRADATIONS: Counter[str] = Counter()


def _strip_secret_params(url: httpx2.URL) -> tuple[httpx2.URL, tuple[tuple[str, str], ...]]:
    """*url* without its credential query params, and the params removed (empty when there were none)."""
    items = url.params.multi_items()
    secrets = tuple((k, v) for k, v in items if is_secret_param(k))
    if not secrets:
        return url, ()
    return url.copy_with(params=httpx2.QueryParams([(k, v) for k, v in items if not is_secret_param(k)])), secrets


async def _close_quietly(*responses: httpx2.Response | None) -> None:
    """Close each response, ignoring failures: this runs on a path that is already recovering from one."""
    for response in responses:
        if response is not None:
            with contextlib.suppress(Exception):
                await response.aclose()


class _ReplayStream(httpx2.AsyncByteStream):
    """The chunks already buffered, then the rest of the original stream, closing the original on ``aclose``."""

    def __init__(self, head: list[bytes], rest: AsyncIterator[bytes], source: httpx2.Response) -> None:
        self._head = head
        self._rest = rest
        self._source = source

    @override
    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self._head:
            yield chunk
        async for chunk in self._rest:
            yield chunk

    @override
    async def aclose(self) -> None:
        await self._source.aclose()


async def _buffer_up_to(response: httpx2.Response, cap: int) -> tuple[httpx2.Response, bytes | None]:
    """Read *response*'s raw body inside the cache layer, up to *cap* bytes, and hand back an equivalent response.

    Raw (undecoded) bytes, so the ``content-encoding`` header stays true of what is handed back and ``read_capped``
    decodes and caps exactly as it does for an uncached client. A body past *cap* is handed back as a replay of what was
    read followed by the unread rest, so ``read_capped`` raises its own ``ResponseTooLargeError`` with the client's
    name rather than this layer inventing one; only the part past the cap is then outside the fallback's reach.

    The body comes back beside the response when it was read whole, for the identical requests waiting on this one to
    share (see :class:`ResilientCacheTransport`), and ``None`` when it was past the cap.
    """
    head: list[bytes] = []
    total = 0
    raw = response.aiter_raw()
    async for chunk in raw:
        head.append(chunk)
        total += len(chunk)
        if total > cap:
            replay = _ReplayStream(head, raw, response)
            return (
                httpx2.Response(
                    response.status_code, headers=response.headers, stream=replay, extensions=response.extensions
                ),
                None,
            )
    await response.aclose()
    body = b"".join(head)
    stream = httpx2.ByteStream(body)
    return (
        httpx2.Response(response.status_code, headers=response.headers, stream=stream, extensions=response.extensions),
        body,
    )


class _RestoreRequestExtensions(httpx2.AsyncBaseTransport):
    """Puts back the request extensions hishel drops on its way down.

    hishel does not forward the request it was handed: it converts to its own ``Request``,
    keeps only the metadata it recognises, and rebuilds an ``httpx2.Request`` from that
    (``extensions=value.metadata``). Everything else is gone — **including httpx2's own
    ``timeout``**, which is not a client setting that arrives some other way but the
    per-request extension httpx2 builds from ``AsyncClient(timeout=...)`` and the only thing
    httpcore reads: ``request.extensions.get("timeout", {})``, defaulting to NO timeout.

    Against a socket that accepts and never answers, a plain client raised ``ClientTimeoutError`` after its configured
    timeout while a cache-enabled client without this layer waited forever: a cache MISS to a black-holing host hung,
    which is the failure a timeout exists to prevent.

    ``setdefault``, so anything hishel computed for itself (``hishel_ttl`` and friends)
    wins over the copy we carry.

    **It also puts back the credential query params** the cache layer stripped before hishel saw the request. hishel
    persists the full request URL with every entry and matches a hit by comparing it, so a key left on the URL (an
    ``api_key=`` parameter) would be written in plaintext into the cache. Stripped above and restored here, the key
    never reaches storage, a hit still matches (both sides are the stripped URL), and the network request still carries
    it.

    **And it records every response it hands up**, so the cache layer's fallback can close one hishel abandoned. hishel
    sends first and stores second; when the store raised, the network response was never read or closed, and its pooled
    connection stayed checked out. After ``max_connections`` storage faults every cache MISS waited out the pool
    timeout.

    This sits directly below the cache transport rather than inside it: hishel's own
    conversion helpers are private, and a transport of our own asserts nothing about their
    shape.
    """

    def __init__(self, inner: httpx2.AsyncBaseTransport) -> None:
        self._inner = inner

    @override
    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        for key, value in (_ORIGINAL_EXTENSIONS.get() or {}).items():
            request.extensions.setdefault(key, value)
        secrets = _STRIPPED_SECRETS.get()
        if secrets:
            request.url = request.url.copy_merge_params(dict(secrets))
        response = await self._inner.handle_async_request(request)
        handed_up = _NETWORK_RESPONSES.get()
        if handed_up is not None:
            handed_up.append(response)
        return response

    @override
    async def aclose(self) -> None:
        await self._inner.aclose()


# The resilient cache transport (a storage fault must not fail the fetch).


@dataclass(frozen=True, slots=True)
class _Download:
    """A finished GET held whole, which every identical request that waited on it gets a copy of."""

    status_code: int
    headers: httpx2.Headers
    body: bytes
    extensions: dict[str, Any]

    def response(self) -> httpx2.Response:
        """Build a response of this download's own, for one waiting request to read."""
        return httpx2.Response(
            self.status_code,
            headers=self.headers,
            stream=httpx2.ByteStream(self.body),
            extensions=dict(self.extensions),
        )


class _Unshared(enum.Enum):
    """Why a finished GET has nothing to hand the identical requests that waited on it."""

    FETCH_OWN = enum.auto()
    """Its body was past the cap, so not held: each waiting request fetches its own."""

    TRY_AGAIN = enum.auto()
    """It was cancelled: the first waiting request to go again fetches for the rest."""


def _sharing_key(request: httpx2.Request) -> str:
    """Return what two in-flight GETs must agree on to share one download: the URL and every request header.

    Headers, not just the URL: two GETs that differ in ``Range``, ``Accept`` or a credential ask for different
    responses.
    """
    return "\n".join([str(request.url), *(f"{k}: {v}" for k, v in sorted(request.headers.multi_items()))])


def _mark_retrieved(download: asyncio.Future[Any]) -> None:
    """Retrieve a failed download's exception, so one that no request waited on is not reported as never retrieved."""
    if not download.cancelled():
        download.exception()


class ResilientCacheTransport(AsyncCacheTransport):
    """``AsyncCacheTransport`` that redoes the request uncached on a STORAGE fault.

    The HTTP cache is an optimization, so a fault in the hishel storage (a network-backed store timing out, a decode
    fault) must never fail a request that would otherwise succeed.

    Only NON-``httpx2`` exceptions trigger the fallback: a genuine network/protocol failure is an
    :class:`httpx2.HTTPError` and is re-raised untouched, so the retry + breaker layers ABOVE this transport keep
    behaving exactly as before. A storage error is counted and the request is re-issued straight at ``next_transport``.

    Re-issuing is safe because the cached surface is read-only BY CONSTRUCTION: policies cover GETs and, at most, a
    body-keyed query POST; mutations are never cached, so a replay costs one extra fetch and nothing else.

    **The body is read INSIDE this layer**, up to the client's size cap. A network-backed hishel storage touches its
    backend while the body streams, which would otherwise happen in ``read_capped``, after this method had
    returned, so a storage fault would escape as a raw backend exception rather than a ``ClientError``. Reading here
    puts that fault inside the fallback. Every response abandoned on the way (hishel's, and the network response it had
    not closed) is closed before the request is redone.

    **It never closes the storage.** One storage is meant to be shared by many clients, so whoever created it closes
    it; ``aclose()`` closes only the transport below.

    **It stamps the client's default TTL**: a client whose :class:`HttpCache` carries a ``ttl`` has it
    set as the request's ``hishel_ttl`` on every request that enters this layer, unless the request already carries
    one (a per-call ``cache_ttl=``). The policy, not the TTL, decides what is stored.

    **Simultaneous identical GETs share one download.** hishel has no request coalescing: an identical request that
    arrives while the first is still fetching finds nothing in storage and goes to the network too, so five concurrent
    lookups of one large file on a cold cache would make five downloads. So the first GET for a URL fetches, and every
    identical GET that arrives while it is in flight waits for it and gets its own copy of that response: a hit or a
    miss, cached or not, since the body is held whole here anyway. A network failure fails every waiting request from
    the one attempt; each caller's retry layer, above this one, then retries, and those retries share again. A first
    request that is cancelled hands the fetch to the next waiting one. A body past the cap is not held, so each waiting
    request fetches its own. POSTs are never shared, since a POST's body is part of what it asks for.
    """

    def __init__(
        self,
        next_transport: httpx2.AsyncBaseTransport,
        storage: AsyncBaseStorage | None = None,
        policy: CachePolicy | None = None,
        *,
        max_body_bytes: int,
        ttl: float | None = None,
    ) -> None:
        # The restorer is installed HERE, so it exists exactly when the cache does and the
        # stack below is otherwise untouched.
        super().__init__(_RestoreRequestExtensions(next_transport), storage=storage, policy=policy)
        self._max_body_bytes = max_body_bytes
        self._ttl = ttl
        # The GETs in flight, by URL, each with what its waiting requests will get.
        self._in_flight: dict[str, asyncio.Future[_Download | _Unshared]] = {}

    @override
    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        # The client's default lifetime. setdefault, so a per-call ``cache_ttl=`` (stamped by
        # AsyncBaseClient._request before the stack) wins. Stamped here rather than on the request's way in because
        # only a cached client has a TTL to give, and this transport exists exactly when the cache does.
        if self._ttl is not None:
            request.extensions.setdefault("hishel_ttl", self._ttl)
        if request.method != "GET":
            response, _ = await self._fetch(request)
            return response
        key = _sharing_key(request)
        while (in_flight := self._in_flight.get(key)) is not None:
            # Shielded: a waiting request that is cancelled must not cancel the fetch the others wait on.
            outcome = await asyncio.shield(in_flight)
            if isinstance(outcome, _Download):
                return outcome.response()
            if outcome is _Unshared.FETCH_OWN:
                response, _ = await self._fetch(request)
                return response
        download: asyncio.Future[_Download | _Unshared] = (
            asyncio.get_running_loop().create_future()
        )  # typeshed types loop.create_future() as Future[Any]
        download.add_done_callback(_mark_retrieved)
        self._in_flight[key] = download
        try:
            response, body = await self._fetch(request)
        except httpx2.HTTPError as exc:
            self._finish(key, exc)
            raise
        except BaseException:
            self._finish(key, _Unshared.TRY_AGAIN)
            raise
        if body is None:
            self._finish(key, _Unshared.FETCH_OWN)
        else:
            self._finish(
                key, _Download(response.status_code, httpx2.Headers(response.headers), body, dict(response.extensions))
            )
        return response

    @override
    async def aclose(self) -> None:
        # Not super().aclose(): hishel's also closes the storage, which other clients may share.
        await self.next_transport.aclose()

    def _finish(self, key: str, outcome: _Download | _Unshared | httpx2.HTTPError) -> None:
        """Hand the requests waiting on *key* what they get, freeing *key* first so none of them finds it again."""
        download = self._in_flight.pop(key)
        if isinstance(outcome, httpx2.HTTPError):
            download.set_exception(outcome)
        else:
            download.set_result(outcome)

    async def _fetch(self, request: httpx2.Request) -> tuple[httpx2.Response, bytes | None]:
        """Fetch *request* through the cache: the response, and its body when it was held whole."""
        # The credential params come off for the duration of this call only and go back in ``finally``: the retry
        # layer above re-sends this SAME request object, and a second attempt must still carry the key.
        original_url = request.url
        stripped_url, secrets = _strip_secret_params(original_url)
        extensions_token = _ORIGINAL_EXTENSIONS.set(request.extensions)
        secrets_token = _STRIPPED_SECRETS.set(secrets)
        request.url = stripped_url
        try:
            return await self._handle(request)
        finally:
            request.url = original_url
            _STRIPPED_SECRETS.reset(secrets_token)
            _ORIGINAL_EXTENSIONS.reset(extensions_token)

    async def _handle(self, request: httpx2.Request) -> tuple[httpx2.Response, bytes | None]:
        handed_up: list[httpx2.Response] = []
        responses_token = _NETWORK_RESPONSES.set(handed_up)
        response: httpx2.Response | None = None
        try:
            response = await super().handle_async_request(request)
            return await _buffer_up_to(response, self._max_body_bytes)
        except httpx2.HTTPError:
            # A real transport failure, not the cache's fault: re-raise it for the retry and breaker layers above,
            # after closing what it abandoned.
            await _close_quietly(response, *handed_up)
            raise
        except Exception as exc:  # noqa: BLE001  # a cache fault of any kind degrades to the live transport
            # The cache broke but the request is still serviceable: redo it on the live transport. Counted BEFORE the
            # live fetch, so a fallback that then fails on the network too cannot hide the cache outage. The query is
            # dropped from the extension, since a key-in-the-URL provider carries its credential there.
            await _close_quietly(response, *handed_up)
            CACHE_DEGRADATIONS[request.url.host] += 1
            fallback = await self.next_transport.handle_async_request(request)
            fallback.extensions[CACHE_DEGRADED_EXT] = (
                f"{request.url.copy_with(query=None, userinfo=b'')}: {type(exc).__name__}: {exc}"[:240]
            )
            return fallback, None
        finally:
            _NETWORK_RESPONSES.reset(responses_token)
