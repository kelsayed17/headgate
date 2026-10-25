# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""The cache transport: hishel's, made safe to share between credentials and to fail.

`hishel <https://hishel.com>`_'s :class:`hishel.httpx2.AsyncCacheTransport` sits below retry and above the rate
budget (see :mod:`headgate._stack`), so a cache hit short-circuits before the retry layer and spends no rate token.
``AsyncBaseClient`` marks a hit so the circuit breaker records neither a success nor a failure for it; admission is
still decided before the fetch, so a hit is refused while the breaker is open.

**Credentials.** hishel sees the request through the client's :class:`~headgate.cache._credentials.CredentialScope`:
no credential header, parameter or userinfo, and a partition label in the URL fragment. The layer below hishel puts
the real URL and headers back for the network, so what is sent is exactly what the caller built.

**Lifetimes.** A client's constant TTL rides the ``hishel_ttl`` request extension: :class:`ResilientCacheTransport`
stamps :class:`~headgate.cache.HttpCache`'s ``ttl``, and ``AsyncBaseClient`` stamps a per-call ``cache_ttl=``, which
wins.

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
from collections import Counter
from collections.abc import Iterator
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, override

import httpx2
from hishel import AsyncBaseStorage, Entry, Request, Response
from hishel.httpx2 import AsyncCacheTransport

from headgate._core import CacheOutcome, CacheResult, CallContext
from headgate.cache._credentials import CredentialScope
from headgate.retry._policy import NETWORK_ERRORS, RETRY_POLICY

if TYPE_CHECKING:
    import uuid
    from collections.abc import AsyncIterator, Callable, Generator, Mapping

    from headgate.cache._policy import HttpCache


__all__ = [
    "CACHE_DEGRADATIONS",
    "CACHE_DEGRADED_EXT",
    "HOST_UNTESTED",
    "CacheStorageError",
    "ResilientCacheTransport",
    "host_untested",
]


# Storage faults.


class CacheStorageError(Exception):
    """A failure inside the cache storage, raised from the backend's own exception.

    Its message is the backend exception's type name alone; the backend's text, which may carry a DSN, stays on the
    chained cause.
    """


#: What passes through the guard on a storage call: only an httpx2 error, which no backend raises for itself.
_PASS_FROM_STORAGE: tuple[type[Exception], ...] = (httpx2.HTTPError,)


@contextlib.contextmanager
def _storage_faults(passing: tuple[type[Exception], ...] = _PASS_FROM_STORAGE) -> Generator[None]:
    """Raise any failure in the block as :class:`CacheStorageError`, except one of *passing*.

    An ``httpx2.HTTPError`` passes through because a storage that stores a miss streams the network body while writing
    it, so a network failure can surface from inside the storage and must still reach the retry layer as one. The body
    of a miss passes every one of :data:`~headgate.retry._policy.NETWORK_ERRORS`, an ``OSError`` from a custom transport
    included; anywhere else an ``OSError`` comes from the backend (a full disk) and is a storage fault.
    """
    try:
        yield
    except passing:
        raise
    except Exception as exc:
        raise CacheStorageError(type(exc).__name__) from exc


async def _guarded_chunks(
    chunks: Iterator[bytes] | AsyncIterator[bytes], passing: tuple[type[Exception], ...]
) -> AsyncIterator[bytes]:
    """Yield *chunks*, a body a storage streams, raising a failure while it streams as :class:`CacheStorageError`."""
    with _storage_faults(passing):
        if isinstance(chunks, Iterator):
            msg = "an async storage returned a synchronous body stream"
            raise TypeError(msg)
        async for chunk in chunks:
            yield chunk


def _guarded_body(entry: Entry, passing: tuple[type[Exception], ...] = _PASS_FROM_STORAGE) -> Entry:
    """Return *entry* with its response body streamed through :func:`_guarded_chunks`."""
    stream = _guarded_chunks(entry.response.stream, passing)
    return dataclasses.replace(entry, response=dataclasses.replace(entry.response, stream=stream))


class _GuardedStorage(AsyncBaseStorage):
    """Delegate to *backend*, raising every backend failure, in a call or in a body it streams, as one error.

    This is the only place a storage fault is recognised, so :class:`ResilientCacheTransport` falls back on exactly
    :class:`CacheStorageError` and lets anything else raised inside hishel propagate. ``close`` is delegated but never
    called by the transport, since a shared storage is closed by whoever created it.
    """

    def __init__(self, backend: AsyncBaseStorage) -> None:
        self._backend = backend

    @override
    async def create_entry(self, request: Request, response: Response, key: str, id_: uuid.UUID | None = None) -> Entry:
        with _storage_faults():
            entry = await self._backend.create_entry(request, response, key, id_)
        # The new entry's body is the network body, teed into the backend as it streams.
        return _guarded_body(entry, NETWORK_ERRORS)

    @override
    async def get_entries(self, key: str) -> list[Entry]:
        with _storage_faults():
            entries = await self._backend.get_entries(key)
        return [_guarded_body(entry) for entry in entries]

    @override
    async def update_entry(self, id: uuid.UUID, new_entry: Entry | Callable[[Entry], Entry]) -> Entry | None:
        with _storage_faults():
            return await self._backend.update_entry(id, new_entry)

    @override
    async def remove_entry(self, id: uuid.UUID) -> None:
        with _storage_faults():
            await self._backend.remove_entry(id)

    @override
    async def refresh_entry_ttl(self, id: uuid.UUID) -> None:
        with _storage_faults():
            await self._backend.refresh_entry_ttl(id)

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

    extensions: Mapping[str, object]
    """The request's own extensions, which hishel drops."""

    handed_up: list[httpx2.Response] = field(default_factory=list)
    """Every network response the layer below returned, for the cache layer to close if hishel abandons one."""

    set_cookies: list[str] = field(default_factory=list)
    """The ``Set-Cookie`` values of the latest network response, kept from hishel and handed back to the caller."""


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
    """Undo, below hishel, what hishel and the cache layer took off the request, and record what comes back.

    hishel does not forward the request it is handed: it rebuilds an ``httpx2.Request`` from its own model, keeping
    only the extensions it recognises. The rest is gone, including httpx2's ``timeout``, the only place httpcore reads
    a timeout from, so without this layer a cache miss to a host that accepts and never answers waits forever. The
    extensions go back with ``setdefault``, so what hishel computed for itself (``hishel_ttl`` and friends) wins.

    The URL and headers go back as the caller sent them. :class:`ResilientCacheTransport` hands hishel a view without
    credentials (:meth:`~headgate.cache._credentials.CredentialScope.view`), because hishel persists the request with
    every entry and matches a hit by comparing its URL; the network still needs them, in their original order and
    multiplicity. A ``FilterPolicy`` adds no request header of its own, so the caller's headers go back whole.

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
        for key, value in call.extensions.items():
            request.extensions.setdefault(key, value)
        request.url = call.url
        request.headers = call.headers
        response = await self._inner.handle_async_request(request)
        call.handed_up.append(response)
        call.set_cookies = response.headers.get_list("set-cookie")
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
            replay = _ReplayStream(head, raw, sources)
            over_cap = httpx2.Response(
                response.status_code, headers=response.headers, stream=replay, extensions=response.extensions
            )
            return over_cap, None
    await _close_quietly(*sources)
    body = b"".join(head)
    whole = httpx2.Response(
        response.status_code, headers=response.headers, stream=httpx2.ByteStream(body), extensions=response.extensions
    )
    return whole, body


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

#: What two in-flight GETs must agree on to share one download: the URL, every request header, the timeouts and the
#: retry policy.
type _SharingKey = tuple[str, tuple[tuple[str, str], ...], tuple[tuple[str, object], ...], object]


@dataclass(slots=True)
class _InFlight:
    """A GET being fetched, and what the identical requests waiting on it get once it finishes."""

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
    responses. The ``timeout`` extension and the call's own retry policy too, since a request willing to wait longer,
    or to try more often, must not inherit the ``ReadTimeout`` of one that was not.
    """
    timeout = request.extensions.get("timeout")
    # Sorted by name, which is unique, so the values (a float or None) are never compared.
    named: list[tuple[str, object]] = sorted((str(name), value) for name, value in timeout.items()) if timeout else []
    timeouts = tuple(named)
    retry = CallContext.of(request).get(RETRY_POLICY)
    return str(request.url), tuple(sorted(request.headers.multi_items())), timeouts, retry


class ResilientCacheTransport(AsyncCacheTransport):
    """hishel's ``AsyncCacheTransport``, made safe to share between clients and to fail.

    **A storage fault degrades to the live transport.** The storage is wrapped so that any backend failure, in a call
    or while a stored body streams, arrives here as :class:`CacheStorageError`; the request is then counted and redone
    straight at the transport below the cache. A network failure is an ``httpx2.HTTPError`` and is re-raised untouched,
    so the retry and breaker layers above see it as they would without a cache. Redoing is safe because the cached
    surface is read-only: policies cover GETs and at most a body-keyed query POST, never a mutation.

    **The body is read inside this layer**, up to the client's size cap. A storage streams a stored body from its
    backend, and stores a miss's body as it streams, so reading here keeps a fault while it streams inside the
    fallback. Every response abandoned on the way is closed before the request is redone. A degraded response is read
    the same way.

    **The storage is never closed here.** One storage may serve many clients, so whoever created it closes it;
    ``aclose()`` closes only the transport below.

    **Credentials never reach the storage**, and each credential's entries stay its own: hishel sees *scope*'s view
    of every request (:class:`~headgate.cache._credentials.CredentialScope`). A transport built without one caches as a
    client without auth would.

    **The client's default TTL** (:class:`~headgate.cache.HttpCache`'s ``ttl``) is stamped as ``hishel_ttl`` on each
    request that does not already carry one from a per-call ``cache_ttl=``. The policy, not the TTL, decides what is
    stored.

    **Simultaneous identical GETs share one download.** hishel has no request coalescing, so an identical GET that
    arrives while the first is still fetching would find nothing in storage and go to the network too. Instead it waits
    and gets its own copy of the first one's response, whether a hit, a miss or degraded, since the body is held whole
    here anyway. A network failure (one of :data:`~headgate.retry._policy.NETWORK_ERRORS`) fails every waiting request
    from the one attempt, each with its own copy chained to the original; each caller's retry layer then retries, and
    those retries share again. What a waiting request receives is marked :data:`HOST_UNTESTED`, so only the request that
    fetched counts toward the circuit breaker. If the first request is cancelled or fails outside the network, the next
    waiting request fetches for the rest. A body past the cap is not held, so each waiting request fetches its own.
    POSTs are never shared, since a POST's body is part of what it asks for.
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
        super().__init__(_RestoreRequest(next_transport), storage=_GuardedStorage(cache.storage), policy=cache.policy)
        self._scope = CredentialScope() if scope is None else scope
        self._live = next_transport
        self._max_body_bytes = max_body_bytes
        self._ttl = cache.ttl
        self._in_flight: dict[_SharingKey, _InFlight] = {}

    @override
    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        # Stamped here because only a cached client has a default TTL to give; setdefault, so a per-call one wins.
        if self._ttl is not None:
            request.extensions.setdefault("hishel_ttl", self._ttl)
        if request.method != "GET":
            response, _ = await self._fetch(request)
            return response
        key = _sharing_key(request)
        while (in_flight := self._in_flight.get(key)) is not None:
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
        in_flight = _InFlight()
        self._in_flight[key] = in_flight
        try:
            response, body = await self._fetch(request)
            in_flight.outcome = _Unshared.FETCH_OWN if body is None else _Download.of(response, body)
        except NETWORK_ERRORS as exc:
            in_flight.outcome = exc
            raise
        finally:
            # Freed before the waiters wake, so none of them finds this download again.
            del self._in_flight[key]
            in_flight.finished.set()
        return response

    async def _fetch(self, request: httpx2.Request) -> tuple[httpx2.Response, bytes | None]:
        """Fetch *request* through the cache, or live when the storage fails: the response, and its body when held."""
        try:
            fetched = await self._fetch_cached(request)
        except CacheStorageError as fault:
            # Counted before the live fetch, so a fallback that then fails on the network too cannot hide the outage.
            CACHE_DEGRADATIONS[request.url.host] += 1
            _report(request, CacheOutcome.DEGRADED)
            live = await self._live.handle_async_request(request)
            # The query is dropped, since a key-in-the-URL provider carries its credential there.
            live.extensions[CACHE_DEGRADED_EXT] = f"{request.url.copy_with(query=None, userinfo=b'')}: {fault}"[:240]
            try:
                return await _buffer_up_to(live, self._max_body_bytes, sources=(live,))
            except BaseException:
                await _close_quietly(live)
                raise
        _report(request, CacheOutcome.HIT if fetched[0].extensions.get("hishel_from_cache") else CacheOutcome.MISS)
        return fetched

    async def _fetch_cached(self, request: httpx2.Request) -> tuple[httpx2.Response, bytes | None]:
        """Fetch *request* through hishel, closing every response it abandons when anything goes wrong."""
        call = _CacheCall(url=request.url, headers=request.headers, extensions=request.extensions)
        token = _CALL.set(call)
        # Restored in ``finally``: the retry layer above re-sends this same request object, credentials and all.
        request.url, request.headers = self._scope.view(request)
        response: httpx2.Response | None = None
        try:
            response = await super().handle_async_request(request)
            if call.set_cookies:
                cookies = [("set-cookie", value) for value in call.set_cookies]
                response.headers = httpx2.Headers([*response.headers.multi_items(), *cookies])
            return await _buffer_up_to(response, self._max_body_bytes, sources=(response, *call.handed_up))
        except BaseException:
            await _close_quietly(response, *call.handed_up)
            raise
        finally:
            request.url = call.url
            request.headers = call.headers
            _CALL.reset(token)
