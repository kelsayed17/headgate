# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""The base client: :class:`AsyncBaseClient`, a policy layer over ``httpx2.AsyncClient``.

httpx2 does the URL joining, headers, timeouts and connection pooling. This adds retries, rate limiting, a per-host
circuit breaker, a deadline, a response-size cap, events, and the mapping of every failure onto the ``ClientError``
tree. It orchestrates; each policy lives in its own module.

Each verb packs its arguments into a :class:`_Call`, and the call then runs in four steps:

1. :meth:`AsyncBaseClient._prepare` renders the URL, builds the ``httpx2.Request`` once, and stamps the call's
   :class:`~headgate._core.CallContext` on it (method, sanitized URL, route template, deadline, retry override,
   idempotency key, redirect choice);
2. :meth:`AsyncBaseClient._reported` and :meth:`AsyncBaseClient._deadline` emit ``CallStarted`` / ``CallFinished``
   around the call and bound it by its deadline;
3. :meth:`AsyncBaseClient._exchange` sends the request under the host's circuit breaker through the transport stack
   (:func:`headgate._stack.build_transport_stack`) and maps a transport failure or a surviving 429/5xx onto a
   ``ClientError`` (:mod:`headgate._failures`);
4. :meth:`AsyncBaseClient._request` caps the body (:func:`headgate._body.read_capped`) and reads the response
   through the call's declared responses (:mod:`headgate._responses`).

The six verbs are written out rather than generated so that each keeps an exact signature for type checkers, IDEs
and subclasses that override one; every body is a single call into :meth:`AsyncBaseClient._request`.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, replace
from string.templatelib import Template
from typing import TYPE_CHECKING, Literal, Self, TypedDict, Unpack

import httpx2

from headgate._auth import failed_on_auth_hop
from headgate._body import read_capped, read_head
from headgate._cache import host_untested
from headgate._core import SILENT, CallContext, CallFinished, CallInfo, CallStarted, Emitter, current_timing
from headgate._core import policy_is_disabled as _policy_is_disabled
from headgate._failures import (
    SHAPE_ERRORS,
    CallSite,
    is_transient_status,
    loggable_url,
    parse_error,
    refusal_error,
    shorten,
    status_error,
    transient_status_error,
    transport_error,
)
from headgate._options import (
    KEYED_METHODS,
    HttpOptions,
    Timeouts,
    TransportPolicy,
    httpx_timeout,
    match_route,
    reject_upstream_httpx,
)
from headgate._responses import Expect, Reader, RefusedError
from headgate._stack import build_http_client, build_transport_stack
from headgate._url import render_path, route_template
from headgate.breaker import BreakerOpenError
from headgate.breaker._registry import NO_BREAKER
from headgate.errors import CircuitOpenError, ClientError, ClientTimeoutError, ForbiddenDestinationError
from headgate.retry import NO_RETRY, RetryPolicy
from headgate.retry._policy import NETWORK_ERRORS, RETRY_POLICY

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator, Mapping
    from types import TracebackType

    from headgate._options import Route
    from headgate._responses import RefusalCheck
    from headgate.breaker import Verdict
    from headgate.breaker._registry import Breaker
    from headgate.ratelimit import Rate

__all__ = ["AsyncBaseClient", "RequestOptions"]

#: How much of a refused or failed streamed response is read, for the ``ClientError``'s body snippet.
_STREAM_ERROR_HEAD_BYTES = 4096

type _Method = Literal["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"]

#: The option groups a client gets when it passes none: frozen, so one instance serves every client.
_DEFAULT_POLICY = TransportPolicy()
_DEFAULT_HTTP = HttpOptions()

#: A query value httpx2 encodes itself.
type _QueryValue = str | int | float | bool | None


class RequestOptions(TypedDict, total=False):
    """The per-call options a verb takes beyond its URL, ``expect=`` and the three explicit overrides.

    * ``params``: the query, ``Mapping[str, object]`` so that a client can pass a TypedDict (a TypedDict is assignable
      to no narrower mapping). Its values are encoded the way httpx2 encodes a mapping: a list or tuple repeats the key
      once per item, and any value that is not a ``str``, number, bool or ``None`` is sent as its ``str()``.
    * ``headers``: added to the client's; a clash is won by the call.
    * ``json`` / ``data``: a JSON body (build it with a codec flavor's ``to_builtins``) or a form body.
    * ``timeout``: :class:`~headgate.Timeouts` replacing the client's, or a number replacing its read timeout.
    * ``deadline``: seconds this call may take in all, or ``None`` for no deadline; absent, the route's or the
      policy's applies.
    * ``idempotency_key``: the idempotency key this call carries on every attempt, under the policy's
      ``IdempotencyKeys`` header (``Idempotency-Key`` by default).
    """

    params: Mapping[str, object] | None
    headers: Mapping[str, str]
    json: object
    data: Mapping[str, object]
    timeout: Timeouts | float
    deadline: float | None
    idempotency_key: str


def _query_pairs(params: Mapping[str, object]) -> list[tuple[str, _QueryValue]]:
    """Flatten *params* into the ``(key, value)`` pairs httpx2 encodes, exactly as httpx2 flattens a mapping."""
    pairs: list[tuple[str, _QueryValue]] = []
    for key, value in params.items():
        items = value if isinstance(value, list | tuple) else (value,)
        pairs.extend((key, _query_value(item)) for item in items)
    return pairs


def _query_value(value: object) -> _QueryValue:
    """Return *value* if httpx2 encodes it natively, else its ``str()``, which is what httpx2 would send."""
    return value if isinstance(value, str | int | float | bool | None) else str(value)


@dataclass(frozen=True, slots=True)
class _Call:
    """One call as a verb received it: what to send, and the per-call overrides."""

    method: _Method
    url: str | Template
    options: RequestOptions
    retry: bool | RetryPolicy = True
    follow_redirects: bool | None = None
    cache_ttl: float | None = None


@dataclass(frozen=True, slots=True)
class _Prepared:
    """A built request, its call's context, and whether its redirects are followed."""

    request: httpx2.Request
    context: CallContext
    follow_redirects: bool


class _Report:
    """The final status of a call, recorded for its ``CallFinished`` event once a response arrives."""

    __slots__ = ("status_code",)

    def __init__(self) -> None:
        """Start with no status."""
        self.status_code: int | None = None


class AsyncBaseClient:
    """Async base for HTTP API clients. Configured through ``__init__``, which builds the transport stack once.

    **Failure policy.** Every verb takes the call's declared responses as ``expect=`` and returns what the matching
    reader read, or raises a :class:`~headgate.errors.ClientError` subtype:

    * ``TransportError`` / ``ClientTimeoutError``: a network failure or timeout, or the call's deadline passing;
    * ``CircuitOpenError``: the host's breaker refused the call without sending it;
    * ``RateLimitError``: a 429 that survived retries, or a quota refusal a reader found inside a 2xx;
    * ``UpstreamStatusError``: a 5xx that survived retries, a status the call did not declare, or a refusal carrying
      the status it arrived with (401/403 become ``AuthError``);
    * ``ResponseParseError``: a 2xx body the reader could not read;
    * ``ResponseTooLargeError``: a body past ``HttpOptions.max_response_bytes``.

    There is no ``None`` return except from a reader that returns it (:func:`~headgate._responses.nothing`): every
    undeclared non-2xx raises, a 404 included. A non-2xx the provider documents as an answer is declared with
    :meth:`Expect.on <headgate._responses.Expect.on>` and comes back as its own wire type.

    **Per-call overrides.** ``retry=False`` sends once and never re-sends, which is what a non-idempotent write
    (placing an order) needs, since every method is retried by default; ``retry=RetryPolicy(...)`` replaces the
    client's policy for the call. ``follow_redirects=False`` makes a redirect raise as its 3xx. ``cache_ttl=`` sets
    this request's cache lifetime. ``timeout=``, ``deadline=`` and ``idempotency_key=`` ride :class:`RequestOptions`.

    **URLs.** A t-string URL (``t"series/{series_id}"``) is rendered by :func:`headgate._url.render_path`, which
    escapes every interpolation as one path segment and raises ``ValueError`` for a value no segment can hold (as
    :func:`~headgate._url.escape_segment` does): that is a programming error at the call site. Its template is the
    call's route in events. A ``str`` URL is passed through verbatim, so a paginator can forward an absolute cursor URL
    from a response body; one httpx2 cannot parse, or a redirect joined past its length limit, raises the root
    ``ClientError``, since such a URL often comes from the wire rather than the code. Every message names a URL
    redacted and cut to a few hundred characters.

    **Time.** The client takes its clock and sleep from :func:`headgate._core.current_timing` when it is built.
    """

    def __init__(
        self,
        *,
        base_url: str,
        rate: Rate,
        auth: httpx2.Auth | None = None,
        policy: TransportPolicy = _DEFAULT_POLICY,
        http: HttpOptions = _DEFAULT_HTTP,
    ) -> None:
        """Build the transport stack for *base_url*, paced at *rate*, signing requests with *auth*.

        *policy* configures retries, the deadline, the breaker, per-path rates, the cache, idempotency keys and
        per-route overrides (:class:`~headgate.TransportPolicy`); *http* the network transport, headers, timeouts,
        redirects, the body cap, cookies, observers and middleware (:class:`~headgate.HttpOptions`).

        :raises TypeError: for an upstream ``httpx`` *auth*.
        """
        reject_upstream_httpx(auth, what="auth")
        # Transport policy off (headgate.policy): no retries and no breaker. The rate limiter checks for itself.
        self._policy_off = _policy_is_disabled()
        if self._policy_off:
            policy = replace(policy, retry=NO_RETRY, breaker=None)
        self._source = type(self).__name__
        self._policy = policy
        self._timing = current_timing()
        self._refusal_check = http.refusal_check
        self._max_response_bytes = http.max_response_bytes
        self._request_id_headers = http.request_id_headers
        self._timeout = http.timeout
        self._emitter = Emitter(http.observers) if http.observers else SILENT
        stack = build_transport_stack(rate, policy, http, self._timing)
        self._http_client = build_http_client(base_url=base_url, auth=auth, http=http, transport=stack)

    async def __aenter__(self) -> Self:
        """Return the client; leaving the block closes it."""
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc_value: BaseException | None, traceback: TracebackType | None
    ) -> None:
        """Close the client."""
        await self.aclose()

    async def aclose(self) -> None:
        """Close the connection pool and the transport stack under it (a cache's storage included)."""
        await self._http_client.aclose()

    # Public API: the verbs.

    async def get[T](
        self,
        url: str | Template,
        *,
        expect: Reader[T] | Expect[T],
        retry: bool | RetryPolicy = True,
        follow_redirects: bool | None = None,
        cache_ttl: float | None = None,
        **options: Unpack[RequestOptions],
    ) -> T:
        """Send a GET and read the reply through *expect*. See :class:`AsyncBaseClient`."""
        call = _Call("GET", url, options, retry=retry, follow_redirects=follow_redirects, cache_ttl=cache_ttl)
        return await self._request(call, expect)

    async def delete[T](
        self,
        url: str | Template,
        *,
        expect: Reader[T] | Expect[T],
        retry: bool | RetryPolicy = True,
        follow_redirects: bool | None = None,
        cache_ttl: float | None = None,
        **options: Unpack[RequestOptions],
    ) -> T:
        """Send a DELETE and read the reply through *expect*. See :class:`AsyncBaseClient`."""
        call = _Call("DELETE", url, options, retry=retry, follow_redirects=follow_redirects, cache_ttl=cache_ttl)
        return await self._request(call, expect)

    async def post[T](
        self,
        url: str | Template,
        *,
        expect: Reader[T] | Expect[T],
        retry: bool | RetryPolicy = True,
        follow_redirects: bool | None = None,
        cache_ttl: float | None = None,
        **options: Unpack[RequestOptions],
    ) -> T:
        """Send a POST and read the reply through *expect*. See :class:`AsyncBaseClient`."""
        call = _Call("POST", url, options, retry=retry, follow_redirects=follow_redirects, cache_ttl=cache_ttl)
        return await self._request(call, expect)

    async def put[T](
        self,
        url: str | Template,
        *,
        expect: Reader[T] | Expect[T],
        retry: bool | RetryPolicy = True,
        follow_redirects: bool | None = None,
        cache_ttl: float | None = None,
        **options: Unpack[RequestOptions],
    ) -> T:
        """Send a PUT and read the reply through *expect*. See :class:`AsyncBaseClient`."""
        call = _Call("PUT", url, options, retry=retry, follow_redirects=follow_redirects, cache_ttl=cache_ttl)
        return await self._request(call, expect)

    async def patch[T](
        self,
        url: str | Template,
        *,
        expect: Reader[T] | Expect[T],
        retry: bool | RetryPolicy = True,
        follow_redirects: bool | None = None,
        cache_ttl: float | None = None,
        **options: Unpack[RequestOptions],
    ) -> T:
        """Send a PATCH and read the reply through *expect*. See :class:`AsyncBaseClient`."""
        call = _Call("PATCH", url, options, retry=retry, follow_redirects=follow_redirects, cache_ttl=cache_ttl)
        return await self._request(call, expect)

    async def head[T](
        self,
        url: str | Template,
        *,
        expect: Reader[T] | Expect[T],
        retry: bool | RetryPolicy = True,
        follow_redirects: bool | None = None,
        cache_ttl: float | None = None,
        **options: Unpack[RequestOptions],
    ) -> T:
        """Send a HEAD and read the reply through *expect*; a HEAD has no body, so ``expect=nothing`` is usual."""
        call = _Call("HEAD", url, options, retry=retry, follow_redirects=follow_redirects, cache_ttl=cache_ttl)
        return await self._request(call, expect)

    async def stream_get(self, url: str | Template, *, follow_redirects: bool | None = None) -> AsyncIterator[bytes]:
        """Send a GET and yield its 2xx body chunk by chunk, never buffered and never capped.

        For a file too large to hold. The chunks are the body after content-decoding, the same bytes a buffered
        ``get`` would see, and the call runs through the same stack and failure ladder, except that:

        * a 2xx is the only success: there is no ``expect=``, and a 3xx raises unless redirects are followed;
        * the client's ``refusal_check`` runs on the response head, before the first chunk;
        * a failure while the body is arriving raises ``TransportError`` or ``ClientTimeoutError`` from inside the
          iteration, after earlier chunks were yielded. A retry covers only the head;
        * the deadline does not apply, since the caller's pace of iteration decides how long the body takes.

        The request goes out when iteration starts, not when this is called. A caller that may stop early should
        iterate under ``contextlib.aclosing`` so the connection is released at once rather than when the generator
        is garbage-collected.
        """
        prepared = self._prepare(_Call("GET", url, {"deadline": None}, follow_redirects=follow_redirects))
        async with self._reported(prepared.context) as report, self._exchange(prepared) as streamed:
            report.status_code = streamed.status_code
            if (refused := _refusal(self._refusal_check, streamed)) is not None:
                head = await read_head(streamed, _STREAM_ERROR_HEAD_BYTES)
                raise refusal_error(refused, head, self._site(prepared)) from refused
            if not streamed.is_success:
                head = await read_head(streamed, _STREAM_ERROR_HEAD_BYTES)
                raise status_error(head, self._site(prepared))
            async for chunk in streamed.aiter_bytes():
                yield chunk

    # The steps of a call.

    def _prepare(self, call: _Call) -> _Prepared:
        """Build *call*'s request and stamp its context on it.

        Raises ``ValueError`` for a t-string value no path segment can hold, for a call that would follow redirects
        on a client with a ``refusal_check``, or for a non-positive ``deadline=``; and ``ClientError`` for a URL httpx2
        cannot parse.
        """
        if isinstance(call.url, Template):
            url, route = render_path(call.url), route_template(call.url)
        else:
            url, route = call.url, None
        info = CallInfo(method=call.method, url=loggable_url(url), route=route)
        follow = self._http_client.follow_redirects if call.follow_redirects is None else call.follow_redirects
        # The per-call half of the constructor's check: the client itself never follows with a refusal_check.
        if self._refusal_check is not None and follow:
            msg = f"{self._source} has a refusal_check=, so no call may follow redirects"
            raise ValueError(msg)
        options = call.options
        params = options.get("params")
        try:
            request = self._http_client.build_request(
                call.method,
                url,
                params=None if params is None else _query_pairs(params),
                headers=options.get("headers"),
                json=options.get("json"),
                data=options.get("data"),
                timeout=httpx_timeout(self._timeout, options.get("timeout")),
            )
        except httpx2.InvalidURL as exc:
            raise _invalid_url(exc, CallSite(self._source, call.method, info.url)) from exc
        route_policy = match_route(self._policy.routes, request.url.path)
        context = CallContext(
            info, clock=self._timing.clock, deadline_after=self._deadline_of(call, route_policy), emitter=self._emitter
        )
        context.follow_redirects = follow
        if (retry := self._retry_of(call, route_policy)) is not None:
            context.put(RETRY_POLICY, retry)
        self._stamp_idempotency_key(request, context, options.get("idempotency_key"))
        # The cache transport only setdefaults the client's own TTL, so a per-call TTL stamped here wins.
        if call.cache_ttl is not None:
            request.extensions["hishel_ttl"] = call.cache_ttl
        context.attach(request)
        return _Prepared(request, context, follow)

    def _deadline_of(self, call: _Call, route: Route | None) -> float | None:
        """Return the seconds *call* may take: its own ``deadline=``, else its route's, else the policy's."""
        if "deadline" in call.options:
            deadline = call.options["deadline"]
            if deadline is not None and not deadline > 0:
                msg = f"deadline must be > 0, or None (got {deadline})"
                raise ValueError(msg)
            return deadline
        if route is not None and route.deadline is not None:
            return route.deadline
        return self._policy.deadline

    def _retry_of(self, call: _Call, route: Route | None) -> RetryPolicy | None:
        """Return *call*'s own retry policy when it overrides the client's: per call, else per route; else ``None``."""
        if self._policy_off or call.retry is False:
            return NO_RETRY
        if isinstance(call.retry, RetryPolicy):
            return call.retry
        return None if route is None else route.retry

    def _stamp_idempotency_key(self, request: httpx2.Request, context: CallContext, key: str | None) -> None:
        """Put the call's idempotency key on *request* once, before the retry layer, so every attempt carries it.

        The key is the call's ``idempotency_key=``, else a fresh one from the policy's ``IdempotencyKeys`` for a POST
        or PATCH. A key the caller already set in ``headers=`` is kept.
        """
        keys = self._policy.idempotency
        header = "Idempotency-Key" if keys is None else keys.header
        if key is None and keys is not None and request.method in KEYED_METHODS:
            key = keys.factory()
        if key is not None:
            request.headers.setdefault(header, key)
        context.idempotency_key = request.headers.get(header)

    def _site(self, prepared: _Prepared) -> CallSite:
        """Return where *prepared*'s call is now, for an error raised about it: the attempt count is read now."""
        info = prepared.context.info
        return CallSite(self._source, info.method, info.url, prepared.context.attempt, self._request_id_headers)

    @contextlib.asynccontextmanager
    async def _reported(self, context: CallContext) -> AsyncGenerator[_Report]:
        """Emit ``CallStarted`` and, on the way out, ``CallFinished``; add the call's notes to a ``ClientError``."""
        report = _Report()
        if context.observed:
            context.emit(CallStarted(call=context.info))
        error: str | None = None
        try:
            yield report
        except BaseException as exc:
            error = type(exc).__name__
            if isinstance(exc, ClientError):
                for note in context.notes:
                    exc.add_note(note)
            raise
        finally:
            if context.observed:
                context.emit(
                    CallFinished(
                        call=context.info,
                        status_code=report.status_code,
                        error=error,
                        attempts=context.attempt,
                        elapsed=context.elapsed(),
                    )
                )

    @contextlib.asynccontextmanager
    async def _deadline(self, prepared: _Prepared) -> AsyncGenerator[None]:
        """Bound the block by the call's deadline, raising ``ClientTimeoutError`` when it passes.

        The deadline is absolute on the client's clock; the block is cancelled after the seconds that remained when it
        began, so everything in it (limiter waits, every attempt and backoff, auth hops, the body read) counts.
        """
        remaining = prepared.context.remaining()
        if remaining is None:
            yield
            return
        # Every TimeoutError raised inside the block is an OSError the exchange has already mapped onto a
        # ClientError, so one that reaches here is the deadline's own.
        try:
            async with asyncio.timeout(remaining):
                yield
        except TimeoutError:
            site = self._site(prepared)
            budget = prepared.context.elapsed()
            msg = f"{site.method} {site.url}: deadline exceeded after {budget:.3g}s"
            raise ClientTimeoutError(msg, **site.details()) from None

    def _breaker_for(self, host: str) -> Breaker:
        """Return *host*'s breaker from the policy's registry, or a no-op one when the client has no breaker."""
        policy = self._policy.breaker
        if policy is None:
            return NO_BREAKER
        return policy.registry.breaker_for(
            host, failure_threshold=policy.failure_threshold, recovery_timeout=policy.recovery_timeout
        )

    @contextlib.asynccontextmanager
    async def _exchange(self, prepared: _Prepared) -> AsyncGenerator[httpx2.Response]:
        """Send the request under its host's circuit breaker and yield the streamed response, closing it afterwards.

        The response yielded is anything but a 429 or 5xx: one of those means the retry transport ran out of
        attempts, and it is raised here, so no call can declare either as an answer. A network failure or timeout,
        in the send or in the caller's body read inside the block, becomes ``TransportError`` /
        ``ClientTimeoutError``, an open breaker ``CircuitOpenError``, and a hop the SSRF guard refused
        ``ForbiddenDestinationError``.

        The breaker is driven here rather than by a transport layer because this is the only scope that spans both the
        send and the body read. It is keyed on the host the request is addressed to, so a followed redirect's hops are
        charged to that host. A transport failure and a surviving 429/5xx are host failures. A cache hit, and an
        outcome the cache marked :data:`~headgate._cache.HOST_UNTESTED` (one another identical GET fetched), tested
        nothing, so only the request that fetched counts. Anything else that escapes (an auth flow failing to mint, a
        body over the cap, a cancellation, the deadline) records nothing (see ``CircuitBreaker.track``).
        """
        try:
            with self._breaker_for(prepared.request.url.host).track() as verdict:
                async with self._sent(prepared, verdict) as streamed:
                    yield streamed
        except BreakerOpenError as exc:
            details = self._site(prepared).details(retry_after=exc.retry_after)
            raise CircuitOpenError(str(exc), **details) from None
        except httpx2.InvalidURL as exc:
            # A redirect joined past httpx2's length limit. Not an HTTPError, and not a host failure.
            raise _invalid_url(exc, self._site(prepared)) from exc
        except ForbiddenDestinationError as exc:
            # The SSRF guard refused a hop before it was sent; it names the destination, the site names the call.
            site = self._site(prepared)
            msg = f"{site.method} {site.url}: {exc}"
            raise ForbiddenDestinationError(msg, **site.details()) from None

    @contextlib.asynccontextmanager
    async def _sent(self, prepared: _Prepared, verdict: Verdict) -> AsyncGenerator[httpx2.Response]:
        """Send the request and yield its streamed response, marking *verdict* with what the exchange showed.

        A surviving 429/5xx is raised with a bounded head of its body, read here while the response is still a stream.
        """
        request = prepared.request
        try:
            streamed = await self._http_client.send(request, stream=True, follow_redirects=prepared.follow_redirects)
            try:
                shared = host_untested(streamed)
                if shared or streamed.extensions.get("hishel_from_cache"):
                    verdict.mark_untested()
                if is_transient_status(streamed.status_code):
                    if not shared:
                        verdict.mark_failed()
                    head = await read_head(streamed, _STREAM_ERROR_HEAD_BYTES)
                    raise transient_status_error(head, self._site(prepared))
                yield streamed
            finally:
                # Closing a partly read compressed stream can raise DecodingError; it must not mask the error in flight.
                with contextlib.suppress(httpx2.HTTPError):
                    await streamed.aclose()
        except NETWORK_ERRORS as exc:
            # A hop an auth flow yielded to mint a credential failed: that is the credential host's failure, not this
            # host's. A failure the cache shared from another request's attempt was counted by that request.
            if not (failed_on_auth_hop(exc) or host_untested(exc)):
                verdict.mark_failed()
            error = transport_error(exc, self._site(prepared))
            if isinstance(exc, httpx2.LocalProtocolError):
                # Its message, and so its traceback, quotes the header value it refused, usually the credential.
                raise error from None
            raise error from exc

    async def _request[T](self, call: _Call, expect: Reader[T] | Expect[T]) -> T:
        """Send *call* and read the reply through its declared responses.

        Every response passes four layers, in order:

        1. **transport** (:meth:`_exchange`): a transport failure, a surviving 429/5xx, or an open breaker raises;
        2. **host**: the client's ``refusal_check`` runs on the final response;
        3. **endpoint**: a status the call declared exactly goes to its reader; any other 2xx to the success reader;
        4. **default**: any other non-2xx raises through :func:`~headgate._failures.status_error`.

        A ``RefusedError`` from a reader, or raised or returned by the ``refusal_check``, becomes the ``ClientError``
        its kind names, with the real status. A reader that finds the body is not the shape it expected (a codec's
        ``DecodeError``, or the ``ValueError``, ``LookupError`` or ``TypeError`` hand-written code over
        ``response.json()`` raises) on a 2xx raises ``ResponseParseError`` naming the status and request line; on a
        declared non-2xx it falls through to the status ladder, because a declaration covers only the body shape it
        names.
        """
        spec: Expect[T] = expect if isinstance(expect, Expect) else Expect(expect)
        prepared = self._prepare(call)
        async with self._reported(prepared.context) as report:
            async with self._deadline(prepared), self._exchange(prepared) as streamed:
                resp = await read_capped(streamed, self._max_response_bytes, self._site(prepared))
            report.status_code = resp.status_code
            return self._dispatch(resp, spec, self._site(prepared))

    def _dispatch[T](self, resp: httpx2.Response, spec: Expect[T], site: CallSite) -> T:
        """Read the final *resp* through the host check, the call's declared responses and the status ladder."""
        if (refused := _refusal(self._refusal_check, resp)) is not None:
            raise refusal_error(refused, resp, site) from refused
        read = spec.answers.get(resp.status_code)
        if read is None:
            if not resp.is_success:
                raise status_error(resp, site)
            read = spec.ok
        try:
            return read(resp)
        # The one place a reader's RefusedError is caught: it becomes the ClientError carrying the status the wire sent.
        except RefusedError as refused:
            raise refusal_error(refused, resp, site) from refused
        except SHAPE_ERRORS as exc:
            if resp.is_success:
                raise parse_error(exc, resp, site) from exc
            raise status_error(resp, site) from exc


def _refusal(check: RefusalCheck | None, resp: httpx2.Response) -> RefusedError | None:
    """Run the client's *check* on *resp*, treating a ``RefusedError`` it raises like one it returns."""
    if check is None:
        return None
    try:
        return check(resp)
    except RefusedError as refused:
        return refused


def _invalid_url(exc: httpx2.InvalidURL, site: CallSite) -> ClientError:
    """Build the ``ClientError`` for a URL httpx2 rejected, naming it and httpx2's reason, both bounded."""
    return ClientError(f"invalid URL {site.url}: {shorten(str(exc))}", **site.details())
