# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""The cache transport: hishel's, made safe to share between credentials and to fail.

`hishel <https://hishel.com>`_'s :class:`hishel.httpx2.AsyncCacheTransport` sits below retry and above the rate
budget (see :mod:`headgate._stack`), so a cache hit short-circuits before the retry layer and spends no rate token.
``AsyncBaseClient`` marks a hit so the circuit breaker records neither a success nor a failure for it; admission is
still decided before the fetch, so a hit is refused while the breaker is open.

**Credentials.** hishel sees the request through the client's :class:`~headgate.cache._credentials.CredentialScope`:
no credential header, parameter or userinfo, every other header's value as a keyed digest, and a partition label in the
URL fragment. The layer below hishel puts the real URL and headers back for the network, so what is sent is exactly
what the caller built.

**Stored on accept.** hishel writes a miss to storage as it passes through. Here the storage defers that write: the
response handed up carries it (:func:`store_accepted`), and ``AsyncBaseClient`` makes it only once the call's reader
accepted the response. A refusal inside a 200, an undecodable body or an undeclared status is never stored, so it is not
served again for the entry's lifetime. :class:`~headgate.cache.HttpCache` still decides what may be stored at all: a
request it does not name is sent past hishel, and a status it does not name is never written. A body past the size cap
is not held here, so it is not stored, and neither is a stream's miss, whose body is never held.

**Lifetimes.** An entry's TTL rides hishel's ``caching`` request extension (:func:`stamp_ttl`):
:class:`ResilientCacheTransport` stamps :class:`~headgate.cache.HttpCache`'s ``ttl``, and ``AsyncBaseClient`` stamps a
per-call ``cache_ttl=``, which wins. hishel reports a hit in the response extension of the same name
(:func:`served_from_cache`).

**Events.** Each request this layer answers emits a :class:`~headgate._core.CacheResult` on its call's context: a hit,
a miss (answered by the network, stored or not), a degraded fetch, or a download shared with an identical GET.

**Faults.** A storage fault never fails a request that would otherwise succeed. Every call into the storage, and every
body it streams, goes through a guard that raises any backend failure as :class:`CacheStorageError`; the transport
counts it (:func:`headgate.health.cache_degradations`), redoes the request on the live transport, and marks the response
with :data:`CACHE_DEGRADED_EXT`. Anything else raised inside hishel is a bug, not an outage, and propagates. A backend's
own message can carry its DSN, password included, so nothing a caller can see repeats it: the degraded marker and
every error raised past this layer name only the backend exception's type.

**Outcomes that say nothing about the host.** Two things this layer hands up did not come from this request's own
attempt on the network: a response or network failure another identical GET fetched (see
:class:`ResilientCacheTransport`), and a storage fault while a body too large to hold streams. Both carry
:data:`HOST_UNTESTED` (:func:`host_untested` reads it), so ``AsyncBaseClient`` records neither against the host's
circuit breaker: one upstream failure shared by five waiting requests is one failure, not five.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import enum
import math
import uuid
from collections import Counter
from collections.abc import Iterator
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final, override

import httpx2
from hishel import AsyncBaseStorage, Entry, Request, Response
from hishel.httpx2 import AsyncCacheTransport

from headgate._core import CacheOutcome, CacheResult, CallContext, Slot, check_kind
from headgate.cache._credentials import CredentialScope
from headgate.cache._policy import Partition
from headgate.retry._policy import NETWORK_ERRORS, RETRY_POLICY

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Generator

    from hishel import Headers

    from headgate.cache._policy import HttpCache

__all__ = [
    "CACHE_DEGRADATIONS",
    "CACHE_DEGRADED_EXT",
    "HOST_UNTESTED",
    "STREAMED",
    "TRACE_HEADERS",
    "CacheStorageError",
    "ResilientCacheTransport",
    "host_untested",
    "served_from_cache",
    "stamp_ttl",
    "store_accepted",
]


# hishel's per-request settings and its report.

#: hishel's request extension for one request's cache settings, and its response extension saying what it did.
CACHING_EXT: Final = "caching"


def stamp_ttl(request: httpx2.Request, ttl: float, *, keep: bool = False) -> None:
    """Set *ttl* as the lifetime of the entry *request* stores; with *keep*, a TTL it already carries stays.

    The settings are copied, never changed in place, since a retry re-sends the same request object.

    :raises TypeError: for a *ttl* that is not a number.
    :raises ValueError: for a *ttl* that is not positive and finite, which no entry could live by.
    """
    check_kind("cache_ttl", ttl, (int, float), "a number of seconds")
    if not (ttl > 0 and math.isfinite(ttl)):
        msg = f"cache_ttl must be a positive finite number of seconds (got {ttl})"
        raise ValueError(msg)
    settings = dict(request.extensions.get(CACHING_EXT, {}))
    if not (keep and "ttl" in settings):
        settings["ttl"] = ttl
    request.extensions[CACHING_EXT] = settings


def served_from_cache(response: httpx2.Response) -> bool:
    """Return whether hishel answered *response* from storage, without the network.

    hishel's report also says whether the response was stored; here that is always ``False``, since a miss is written
    only once its call accepts it (:func:`store_accepted`).
    """
    status = response.extensions.get(CACHING_EXT)
    return isinstance(status, dict) and bool(status.get("from_cache"))


# Storage faults.


class CacheStorageError(Exception):
    """A failure inside the cache storage, raised from the backend's own exception.

    Its message is the backend exception's type name alone; the backend's text, which may carry a DSN, stays on the
    chained cause.
    """


@contextlib.contextmanager
def _storage_faults() -> Generator[None]:
    """Raise any failure in the block as :class:`CacheStorageError`.

    The network body of a miss never passes through the backend (see :class:`_GuardedStorage`), so whatever fails
    inside it, an ``OSError`` from a full disk included, is a storage fault.
    """
    try:
        yield
    except Exception as exc:
        raise CacheStorageError(type(exc).__name__) from exc


async def _guarded_chunks(chunks: Iterator[bytes] | AsyncIterator[bytes] | None) -> AsyncIterator[bytes]:
    """Yield *chunks*, a body a storage streams, raising a failure while it streams as :class:`CacheStorageError`.

    A body of ``None``, which hishel's model allows, is an empty one.
    """
    if chunks is None:
        return
    with _storage_faults():
        if isinstance(chunks, Iterator):
            msg = "an async storage returned a synchronous body stream"
            raise TypeError(msg)
        async for chunk in chunks:
            yield chunk


def _guarded_body(entry: Entry) -> Entry:
    """Return *entry* with its response body streamed through :func:`_guarded_chunks`."""
    stream = _guarded_chunks(entry.response.stream)
    return dataclasses.replace(entry, response=dataclasses.replace(entry.response, stream=stream))


def _vary_names(headers: Headers) -> frozenset[str]:
    """Return the header names a response's ``Vary`` lists, lowercased; ``*`` among them when it varies on anything."""
    names = {name.strip().lower() for value in headers.get_list("vary") or () for name in value.split(",")}
    names.discard("")
    return frozenset(names)


def _same_variant(entry: Entry, headers: httpx2.Headers) -> bool:
    """Return whether *entry* answers a request sending *headers*, by the headers its response's ``Vary`` names.

    Each named header must have the values the stored request had (RFC 9111 section 4.1). Both sides are the cache's
    view of a request (see :class:`~headgate.cache._credentials.CredentialScope`), where each value is a keyed digest,
    so equal digests are equal values. No entry varying on ``*``, or on a header the view drops, is ever stored (see
    :meth:`_GuardedStorage.create_entry`), so a named header the view lacks is one neither request sent.
    """
    return all(
        (entry.request.headers.get_list(name) or []) == headers.get_list(name)
        for name in _vary_names(entry.response.headers)
    )


class _GuardedStorage(AsyncBaseStorage):
    """Delegate to *backend*, raising every backend failure, in a call or in a body it streams, as one error.

    This is the only place a storage fault is recognized, so :class:`ResilientCacheTransport` falls back on exactly
    :class:`CacheStorageError` and lets anything else raised inside hishel propagate. ``close`` is delegated but never
    called by the transport, since a shared storage is closed by whoever created it.

    A new entry is not written here: it is held on the request's trip through hishel (:class:`_Deferred`), and written
    by :class:`_Store` once the call's reader accepts the response. One is never held when no later request could be
    told to be its variant or another's: a status *stores* refuses, a ``Vary: *``, and, under a *shared* partition, a
    ``Vary`` naming a header the request sent but the view drops (``Authorization``, a cookie, a declared credential).
    Under a credential partition the view's partition label already carries that header's value, so each value has
    entries of its own.
    """

    def __init__(self, backend: AsyncBaseStorage, *, stores: Callable[[int], bool], shared: bool) -> None:
        super().__init__()
        self._backend = backend
        self._stores = stores
        self._shared = shared

    def _keeps(self, response: Response) -> bool:
        """Return whether *response* may be stored for the request on this trip."""
        if not self._stores(response.status_code):
            return False
        names = _vary_names(response.headers)
        if "*" in names:
            return False
        call = _CALL.get()
        return not (self._shared and any(name in call.headers and name not in call.view_headers for name in names))

    @override
    async def create_entry(
        self, request: Request, response: Response, key: str, *, ttl: float | None = None, tti: float | None = None
    ) -> Entry:
        entry = Entry(id=uuid.uuid4(), request=request, response=response)
        # The body is still the network's, so nothing of the backend's streams here.
        if self._keeps(response):
            _CALL.get().deferred = _Deferred(entry, key, ttl, tti)
        return entry

    @override
    async def get_entries(self, key: str) -> list[Entry]:
        with _storage_faults():
            entries = await self._backend.get_entries(key)
        # hishel keys an entry by its URL alone, so the variants a response's Vary names are told apart here.
        headers = _CALL.get().view_headers
        return [_guarded_body(entry) for entry in entries if _same_variant(entry, headers)]

    @override
    async def update_headers(self, id: uuid.UUID, headers: Headers) -> None:
        with _storage_faults():
            await self._backend.update_headers(id, headers)

    @override
    async def remove_entry(self, id: uuid.UUID) -> None:
        with _storage_faults():
            await self._backend.remove_entry(id)

    @override
    async def close(self) -> None:
        await self._backend.close()


# Carrying the caller's request past hishel.


@dataclass(slots=True)
class _CacheCall:
    """One request's trip through hishel: what the layer below puts back, and what it hands up."""

    url: httpx2.URL
    """The request's own URL, credentials included."""

    headers: httpx2.Headers
    """The request's own headers, credentials included."""

    view_headers: httpx2.Headers
    """The headers hishel sees and stores, which a stored response's ``Vary`` is matched against."""

    handed_up: list[httpx2.Response] = field(default_factory=list)
    """Every network response the layer below returned, for the cache layer to close if hishel abandons one."""

    set_cookies: list[bytes] = field(default_factory=list)
    """The ``Set-Cookie`` values of the latest network response, kept from hishel and handed back to the caller."""

    deferred: _Deferred | None = None
    """The entry hishel asked to store for this request, not yet written."""


@dataclass(frozen=True, slots=True)
class _Deferred:
    """An entry hishel asked to store, whose body is still on its way from the network."""

    entry: Entry
    key: str
    ttl: float | None
    tti: float | None


class _Store:
    """Write a deferred entry with its body, once, the first time a call accepts the response (:func:`store_accepted`).

    Shared by every copy of the response, a download's waiting requests included, so it is written at most once. A
    storage fault while writing, or a write still running when the accepting call's deadline passes, is counted as a
    degradation and dropped: the call already has its answer.
    """

    def __init__(self, backend: AsyncBaseStorage, deferred: _Deferred, body: bytes, host: str) -> None:
        """Hold *deferred* and its whole *body*, to write to *backend*; *host* is counted on a fault."""
        self._backend = backend
        self._deferred = deferred
        self._body = body
        self._host = host
        self._written = False

    async def __call__(self, time_left: float | None) -> None:
        """Write the entry within *time_left* seconds (no bound for ``None``), unless it was written already."""
        if self._written:
            return
        self._written = True
        entry, body = self._deferred.entry, self._body
        response = dataclasses.replace(entry.response, stream=_one_chunk(body))
        try:
            async with asyncio.timeout(time_left):
                with _storage_faults():
                    stored = await self._backend.create_entry(
                        entry.request, response, self._deferred.key, ttl=self._deferred.ttl, tti=self._deferred.tti
                    )
                # A storage writes the body as it streams.
                async for _ in _guarded_chunks(stored.response.stream):
                    pass
        except CacheStorageError, TimeoutError:
            CACHE_DEGRADATIONS[self._host] += 1


async def _one_chunk(body: bytes) -> AsyncIterator[bytes]:
    """Yield *body* as one chunk."""
    yield body


#: The response extension carrying the :class:`_Store` of a miss, for :func:`store_accepted`.
CACHE_STORE = "headgate.cache_store"


async def store_accepted(response: httpx2.Response) -> None:
    """Store the cache miss *response* answers, now that the call accepted it; do nothing for any other response.

    The write is bounded by the time left before the accepting call's deadline, read from the request *response*
    answers when it has one.
    """
    store = response.extensions.get(CACHE_STORE)
    if isinstance(store, _Store):
        try:
            context = CallContext.find(response.request)
        except RuntimeError:  # A response no client sent carries no request.
            context = None
        await store(None if context is None else context.remaining())


#: Marks a call opened as a stream, which this layer hands up as it arrives: never read ahead, shared or stored.
STREAMED = Slot[bool]("streamed")

#: The request extension naming the headers a tracer stamps on each attempt (OpenTelemetry's ``traceparent``, say),
#: which differ on every call and so are left out of what identical GETs must agree on to share a download.
TRACE_HEADERS = "headgate.trace_headers"

#: The call in progress in this task. A ContextVar rather than instance state because one transport serves every
#: concurrent request on a client. Set only around hishel, so the layer below never runs without it.
_CALL: ContextVar[_CacheCall] = ContextVar("headgate_cache_call")

#: The response extension a request that found the cache broken carries: the URL (query and userinfo dropped) and the
#: backend exception's type name.
CACHE_DEGRADED_EXT = "headgate.cache_degraded"

#: The response extension, and the exception attribute, marking an outcome this request's own network attempt did not
#: produce, so the circuit breaker must not record it. See :func:`host_untested`.
HOST_UNTESTED = "headgate.host_untested"

#: How many requests per host found the cache broken and were redone on the live transport, since process start.
#: Process-global like the breaker registry, read through :func:`headgate.health.cache_degradations`.
CACHE_DEGRADATIONS: Counter[str] = Counter()


def host_untested(outcome: httpx2.Response | BaseException) -> bool:
    """Return whether *outcome* says nothing about its host's health, because this layer marked it so.

    True for a response or network failure a request received from another identical GET's fetch, and for the error a
    storage fault raises while an unheld body streams.
    """
    if isinstance(outcome, httpx2.Response):
        return bool(outcome.extensions.get(HOST_UNTESTED))
    return bool(vars(outcome).get(HOST_UNTESTED))


def _mark_host_untested[E: BaseException](exc: E) -> E:
    """Mark *exc* as saying nothing about its host (see :func:`host_untested`) and return it."""
    vars(exc)[HOST_UNTESTED] = True
    return exc


async def _close_quietly(*responses: httpx2.Response | None) -> None:
    """Close each response, suppressing a close failure so it cannot replace the error being handled."""
    for response in responses:
        if response is not None:
            with contextlib.suppress(httpx2.HTTPError):
                await response.aclose()


class _RestoreRequest(httpx2.AsyncBaseTransport):
    """Undo, below hishel, what the cache layer took off the request, and record what comes back.

    The URL and headers go back as the caller sent them for the send, and the view comes back once it returns.
    :class:`ResilientCacheTransport` hands hishel a view without credentials
    (:meth:`~headgate.cache._credentials.CredentialScope.view`), because hishel persists the request with every entry
    and keys a hit by its URL; the network still needs them, in their original order and multiplicity. hishel forwards
    the request object it is handed, extensions included, adds no header of its own, and reads that same object for
    the entry it stores after the send, so the view must be back on it by then: otherwise a credential would be
    stored.

    A response's ``Set-Cookie`` is taken off before hishel sees it, so no stored entry carries one: a cookie is a
    session, the caller's alone, and a hit would otherwise hand it to every client on the storage. The cache layer puts
    it back on the response it hands up for this request.

    Every response handed up is recorded, because hishel sends before it stores: when the store fails, the network
    response it holds is never read or closed, and its pooled connection stays checked out until the cache layer closes
    it.
    """

    def __init__(self, inner: httpx2.AsyncBaseTransport) -> None:
        self._inner = inner

    @override
    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        call = _CALL.get()
        view = request.url, request.headers
        request.url = call.url
        request.headers = call.headers
        try:
            response = await self._inner.handle_async_request(request)
        finally:
            # hishel records the entry's request from this same object once the send returns: give it the view back.
            request.url, request.headers = view
        call.handed_up.append(response)
        # Raw bytes, so a value that is not ASCII goes back exactly as it came.
        call.set_cookies = [value for name, value in response.headers.raw if name.lower() == b"set-cookie"]
        if call.set_cookies:
            del response.headers["set-cookie"]
        return response

    @override
    async def aclose(self) -> None:
        await self._inner.aclose()


class _ReplayStream(httpx2.AsyncByteStream):
    """The chunks already read, then the rest of the original stream; ``aclose`` closes every response behind it."""

    def __init__(self, head: list[bytes], rest: AsyncIterator[bytes], sources: tuple[httpx2.Response, ...]) -> None:
        self._head = head
        self._rest = rest
        self._sources = sources

    @override
    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self._head:
            yield chunk
        try:
            async for chunk in self._rest:
                yield chunk
        except CacheStorageError as fault:
            # Past the cap the fetch's fallback has already returned, so the fault can only fail the read. It is raised
            # as the read failure it is, value-free and unchained, since the backend's text may carry its DSN; and the
            # host is not charged, since the network was not what failed.
            msg = f"the cache storage failed while the body streamed ({fault})"
            raise _mark_host_untested(httpx2.ReadError(msg)) from None

    @override
    async def aclose(self) -> None:
        await _close_quietly(*self._sources)


async def _buffer_up_to(
    response: httpx2.Response, cap: int, *, sources: tuple[httpx2.Response, ...]
) -> tuple[httpx2.Response, bytes | None]:
    """Read *response*'s raw body up to *cap* bytes, and return an equivalent response with the body when read whole.

    Raw (undecoded) bytes, so ``content-encoding`` stays true of what is returned and ``read_capped`` decodes and caps
    exactly as it does for an uncached client. A body past *cap* comes back as a replay of what was read followed by
    the unread rest, with ``None`` for the body, so ``read_capped`` raises its own ``ResponseTooLargeError``. *sources*
    are the responses this one reads through (hishel's, and the network response under it); each is closed once the
    body is read whole, or when the replay is closed. A response whose body is already in memory (one built from
    ``content=``) is returned as it is.
    """
    if response.is_stream_consumed:
        return response, response.content if len(response.content) <= cap else None
    head: list[bytes] = []
    total = 0
    raw = response.aiter_raw()
    async for chunk in raw:
        head.append(chunk)
        total += len(chunk)
        if total > cap:
            return _replayed(response, head, raw, sources), None
    await _close_quietly(*sources)
    body = b"".join(head)
    whole = httpx2.Response(
        response.status_code, headers=response.headers, stream=httpx2.ByteStream(body), extensions=response.extensions
    )
    return whole, body


def _replayed(
    response: httpx2.Response, head: list[bytes], rest: AsyncIterator[bytes], sources: tuple[httpx2.Response, ...]
) -> httpx2.Response:
    """Return *response* reading *head*, then *rest*, and closing every one of *sources* when closed."""
    stream = _ReplayStream(head, rest, sources)
    return httpx2.Response(
        response.status_code, headers=response.headers, stream=stream, extensions=response.extensions
    )


# Sharing one download between simultaneous identical GETs.


@dataclass(frozen=True, slots=True)
class _Download:
    """A finished GET held whole, which every identical request that waited on it gets a copy of."""

    status_code: int
    headers: httpx2.Headers
    body: bytes
    extensions: dict[str, object]

    @classmethod
    def of(cls, response: httpx2.Response, body: bytes) -> _Download:
        """Hold *response*, whose body *body* was read whole, for the requests waiting on it."""
        return cls(response.status_code, httpx2.Headers(response.headers), body, dict(response.extensions))

    def response(self) -> httpx2.Response:
        """Build a response of this download's own for one waiting request to read, marked :data:`HOST_UNTESTED`."""
        return httpx2.Response(
            self.status_code,
            headers=self.headers,
            stream=httpx2.ByteStream(self.body),
            extensions={**self.extensions, HOST_UNTESTED: True},
        )


#: A network failure, as the retry layer defines it (:data:`~headgate.retry._policy.NETWORK_ERRORS`).
type _NetworkError = httpx2.HTTPError | OSError


def _shared_failure(exc: _NetworkError) -> _NetworkError:
    """Return a copy of *exc* for one waiting request to raise, chained to it and marked :data:`HOST_UNTESTED`.

    Each waiting request raises its own copy, so the original's traceback is not extended once per task that re-raises
    it. The copy is built without ``__init__``, as pickling would, so an exception whose constructor takes required
    keywords copies as well as any other; it keeps the type the retry and timeout mapping above read.
    """
    copy = type(exc).__new__(type(exc), *exc.args)
    vars(copy).update(vars(exc))
    if "__notes__" in vars(exc):
        # Its own list, so a note one waiting request adds does not appear on every other copy.
        copy.__notes__ = [*exc.__notes__]
    copy.__cause__ = exc
    return _mark_host_untested(copy)


class _Unshared(enum.Enum):
    """Why a finished GET has nothing to hand the identical requests that waited on it."""

    FETCH_OWN = enum.auto()
    """Its body was past the cap, so not held: each waiting request fetches its own."""

    TRY_AGAIN = enum.auto()
    """It was cancelled or failed outside the network: the first waiting request to go again fetches for the rest."""


type _Outcome = _Download | _Unshared | _NetworkError

#: What two in-flight GETs must agree on to share one download: the URL, every request header, the timeouts, the
#: retry policy and the cache lifetime.
type _SharingKey = tuple[str, tuple[tuple[str, str], ...], tuple[tuple[str, object], ...], object, object]


@dataclass(slots=True)
class _InFlight:
    """A GET being fetched, and what the identical requests waiting on it get once it finishes."""

    context: CallContext | None
    """The call fetching it, whose own later sends (a hedge) never wait on it."""

    finished: asyncio.Event = field(default_factory=asyncio.Event)
    outcome: _Outcome = _Unshared.TRY_AGAIN


def _report(request: httpx2.Request, outcome: CacheOutcome) -> None:
    """Emit a :class:`~headgate._core.CacheResult` with *outcome* on *request*'s call, when anyone is listening."""
    context = CallContext.find(request)
    if context is not None and context.observed:
        context.emit(CacheResult(call=context.info, outcome=outcome))


def _sharing_key(request: httpx2.Request) -> _SharingKey:
    """Return what two in-flight GETs must agree on to share one download.

    Headers as well as the URL, since two GETs that differ in ``Range``, ``Accept`` or a credential ask for different
    responses. The timeouts and the call's own retry policy too, since a request willing to wait longer, or to try more
    often, must not inherit the ``ReadTimeout`` of one that was not. The timeouts are the ones the request asked for,
    not the ones fitted to its deadline, which differ on every request; each waiter's own deadline bounds its wait. And
    the TTL, since the one download is stored once, for as long as the request that fetched it said. A tracer's headers
    (:data:`TRACE_HEADERS`) are left out, since they differ on every call.
    """
    timeouts = CallContext.requested_timeouts(request)
    retry = CallContext.of(request).get(RETRY_POLICY)
    settings = request.extensions.get(CACHING_EXT)
    ttl = settings.get("ttl") if isinstance(settings, dict) else None
    traced = {name.lower() for name in request.extensions.get(TRACE_HEADERS, ())}
    headers = tuple(sorted(item for item in request.headers.multi_items() if item[0] not in traced))
    return str(request.url), headers, timeouts, retry, ttl


def _cut_short(request: httpx2.Request, failure: Exception) -> bool:
    """Return whether *failure* is a timeout of *request*'s own deadline-fitted timeouts, not of the ones it asked for.

    The requests waiting on it asked for the same timeouts, but each has a deadline of its own: one with more time left
    must fetch for itself rather than inherit a timeout it would not have hit. When the failed phase is known, only
    that phase matters; a timeout of no known phase is handed off when any phase was shortened.
    """
    for error_type, phase in (
        (httpx2.ConnectTimeout, "connect"),
        (httpx2.ReadTimeout, "read"),
        (httpx2.WriteTimeout, "write"),
        (httpx2.PoolTimeout, "pool"),
    ):
        if isinstance(failure, error_type):
            requested = dict(CallContext.requested_timeouts(request))
            current = request.extensions.get("timeout")
            return isinstance(current, dict) and phase in current and bool(current[phase] != requested.get(phase))
    # A timeout of no known phase may be any of them: hand off when the deadline shortened any.
    return isinstance(failure, httpx2.TimeoutException) and CallContext.timeouts_cut(request)


class ResilientCacheTransport(AsyncCacheTransport):
    """hishel's ``AsyncCacheTransport``, made safe to share between clients and to fail.

    **A storage fault degrades to the live transport.** The storage is wrapped so that any backend failure, in a call
    or while a stored body streams, arrives here as :class:`CacheStorageError`; the request is then counted and redone
    straight at the transport below the cache. A network failure is an ``httpx2.HTTPError`` and is re-raised untouched,
    so the retry and breaker layers above see it as they would without a cache. Redoing is safe because the cached
    surface is read-only: only a GET or HEAD goes through hishel, never a mutation.

    **The body is read inside this layer**, up to the client's size cap, for a request the rules name. A storage streams
    a stored body from its backend, so reading here keeps a fault while it streams inside the fallback. Every response
    abandoned on the way is closed before the request is redone. A degraded response is read the same way. A request
    the rules do not name (a POST among them) goes to the network and up as it arrives, and so does a stream's miss
    (:data:`STREAMED`): a stream is held only when it is a hit.

    **The storage is never closed here.** One storage may serve many clients, so whoever created it closes it;
    ``aclose()`` closes only the transport below.

    **Credentials never reach the storage**, and each credential's entries stay its own: hishel sees *scope*'s view
    of every request (:class:`~headgate.cache._credentials.CredentialScope`). A transport built without one caches as a
    client without auth would.

    **The client's default TTL** (:class:`~headgate.cache.HttpCache`'s ``ttl``) is stamped (:func:`stamp_ttl`) on each
    request that does not already carry one from a per-call ``cache_ttl=``. :class:`~headgate.cache.HttpCache`'s rules,
    not the TTL, decide what is stored: a request they do not name is sent straight past hishel.

    **Simultaneous identical GETs share one download.** hishel has no request coalescing, so an identical GET that
    arrives while the first is still fetching would find nothing in storage and go to the network too. Instead it waits
    and gets its own copy of the first one's response, whether a hit, a miss or degraded, since the body is held whole
    here anyway. A network failure (one of :data:`~headgate.retry._policy.NETWORK_ERRORS`) fails every waiting request
    from the one attempt, each with its own copy chained to the original; each caller's retry layer then retries, and
    those retries share again. What a waiting request receives is marked :data:`HOST_UNTESTED`, so only the request that
    fetched counts toward the circuit breaker. If the first request is cancelled or fails outside the network, the next
    waiting request fetches for the rest. A body past the cap is not held, so each waiting request fetches its own.
    POSTs are never shared, since a POST's body is part of what it asks for, nor streams, nor a later send of the call
    fetching (a hedge), which would only wait on the send it is meant to overtake.
    """

    def __init__(
        self,
        next_transport: httpx2.AsyncBaseTransport,
        cache: HttpCache,
        *,
        max_body_bytes: int,
        scope: CredentialScope | None = None,
    ) -> None:
        """Cache per *cache* over *next_transport*, holding bodies up to *max_body_bytes*, viewing requests by *scope*.

        Without a *scope*, requests are viewed as a client with no auth would send them.
        """
        shared = cache.partition is Partition.SHARED
        storage = _GuardedStorage(cache.storage, stores=cache.stores_status, shared=shared)
        super().__init__(_RestoreRequest(next_transport), storage=storage)
        self._cache = cache
        self._backend = cache.storage
        self._scope = CredentialScope() if scope is None else scope
        self._live = next_transport
        self._max_body_bytes = max_body_bytes
        self._ttl = cache.ttl
        self._in_flight: dict[_SharingKey, _InFlight] = {}

    @override
    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        # Stamped here because only a cached client has a default TTL to give; setdefault, so a per-call one wins.
        if self._ttl is not None:
            stamp_ttl(request, self._ttl, keep=True)
        if not self._cache.caches_request(request.method, request.url.path):
            # Neither looked up nor stored, so nothing here needs its body: it goes up as it arrives.
            return await self._live.handle_async_request(request)
        context = CallContext.find(request)
        streamed = context is not None and bool(context.get(STREAMED))
        if request.method != "GET" or streamed:
            response, _ = await self._fetch(request, streamed=streamed)
            return response
        key = _sharing_key(request)
        while (in_flight := self._in_flight.get(key)) is not None:
            if context is not None and in_flight.context is context:
                # Another send of the fetching call (a hedge) would only wait on the send it is meant to overtake.
                response, _ = await self._fetch(request)
                return response
            await in_flight.finished.wait()
            outcome = in_flight.outcome
            if isinstance(outcome, NETWORK_ERRORS):
                _report(request, CacheOutcome.SHARED)
                raise _shared_failure(outcome)
            if isinstance(outcome, _Download):
                _report(request, CacheOutcome.SHARED)
                return outcome.response()
            if outcome is _Unshared.FETCH_OWN:
                response, _ = await self._fetch(request)
                return response
        return await self._fetch_for_waiters(key, request)

    @override
    async def aclose(self) -> None:
        # Not super().aclose(), which also closes the storage that other clients may share.
        await self.next_transport.aclose()

    async def _fetch_for_waiters(self, key: _SharingKey, request: httpx2.Request) -> httpx2.Response:
        """Fetch *request*, then hand the identical GETs that arrived meanwhile what they get."""
        in_flight = _InFlight(CallContext.find(request))
        self._in_flight[key] = in_flight
        try:
            response, body = await self._fetch(request)
            in_flight.outcome = _Unshared.FETCH_OWN if body is None else _Download.of(response, body)
        except NETWORK_ERRORS as exc:
            in_flight.outcome = _Unshared.FETCH_OWN if _cut_short(request, exc) else exc
            raise
        finally:
            # Freed before the waiters wake, so none of them finds this download again.
            del self._in_flight[key]
            in_flight.finished.set()
        return response

    async def _fetch(self, request: httpx2.Request, *, streamed: bool = False) -> tuple[httpx2.Response, bytes | None]:
        """Fetch *request* through the cache, or live when the storage fails: the response, and its body when held.

        A *streamed* request's body is held only when it is a hit, so a storage fault while it streams stays inside
        the fallback; a miss or a degraded fetch goes up as it arrives.
        """
        try:
            fetched = await self._fetch_cached(request, streamed=streamed)
        except CacheStorageError as fault:
            # Counted before the live fetch, so a fallback that then fails on the network too cannot hide the outage.
            CACHE_DEGRADATIONS[request.url.host] += 1
            _report(request, CacheOutcome.DEGRADED)
            live = await self._live.handle_async_request(request)
            # The query is dropped, since a key-in-the-URL provider carries its credential there.
            live.extensions[CACHE_DEGRADED_EXT] = f"{request.url.copy_with(query=None, userinfo=b'')}: {fault}"[:240]
            if streamed:
                return live, None
            try:
                return await _buffer_up_to(live, self._max_body_bytes, sources=(live,))
            except BaseException:
                await _close_quietly(live)
                raise
        _report(request, CacheOutcome.HIT if served_from_cache(fetched[0]) else CacheOutcome.MISS)
        return fetched

    async def _fetch_cached(self, request: httpx2.Request, *, streamed: bool) -> tuple[httpx2.Response, bytes | None]:
        """Fetch *request* through hishel, closing every response it abandons when anything goes wrong."""
        view_url, view_headers = self._scope.view(request)
        call = _CacheCall(url=request.url, headers=request.headers, view_headers=view_headers)
        token = _CALL.set(call)
        # Restored in ``finally``: the retry layer above re-sends this same request object, credentials and all.
        request.url, request.headers = view_url, view_headers
        response: httpx2.Response | None = None
        try:
            response = await super().handle_async_request(request)
            # hishel says stored as soon as it asks; nothing is written until the call accepts the response.
            response.extensions[CACHING_EXT] = {**response.extensions[CACHING_EXT], "stored": False}
            if call.set_cookies:
                cookies = [(b"set-cookie", value) for value in call.set_cookies]
                response.headers = httpx2.Headers([*response.headers.raw, *cookies])
            sources = (response, *call.handed_up)
            if streamed and not served_from_cache(response):
                held, body = _replayed(response, [], response.aiter_raw(), sources), None
            else:
                held, body = await _buffer_up_to(response, self._max_body_bytes, sources=sources)
        except BaseException:
            await _close_quietly(response, *call.handed_up)
            raise
        finally:
            request.url = call.url
            request.headers = call.headers
            _CALL.reset(token)
        if call.deferred is not None and body is not None:
            store = _Store(self._backend, call.deferred, body, call.url.host)
            held.extensions = {**held.extensions, CACHE_STORE: store}
        return held, body
