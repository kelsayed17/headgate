# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""The base client: :class:`AsyncBaseClient`, a policy layer over ``httpx2.AsyncClient``.

httpx2 does the URL joining, headers, timeouts and connection pooling. This adds retries, rate limiting, a per-origin
circuit breaker, a deadline, a response-size cap, events, and the mapping of every failure onto the ``ClientError``
tree. It orchestrates; each policy lives in its own module, and the transport stack under ``httpx2.AsyncClient`` is
assembled in :mod:`headgate._stack`.

Every call (a verb, :meth:`~AsyncBaseClient.stream`, each page of :meth:`~AsyncBaseClient.paginate`) is packed into a
:class:`_Call` and runs through the same steps:

1. :meth:`~AsyncBaseClient._prepare` renders the URL, builds the ``httpx2.Request`` once, and stamps the call's
   :class:`~headgate._core.CallContext` on it: method, sanitized URL, route template, deadline, retry override, body
   replayability, idempotency key and redirect choice;
2. :meth:`~AsyncBaseClient._reported` emits ``CallStarted`` and ``CallFinished`` around the call and adds its notes to
   the error it raises; :meth:`~AsyncBaseClient._deadline` bounds it by its deadline;
3. :meth:`~AsyncBaseClient._exchange` sends the request under the origin's circuit breaker, through the transport
   stack, and :meth:`~AsyncBaseClient._sent` maps a network failure or a surviving 429/5xx onto a ``ClientError``
   (:mod:`headgate._failures`), charging the breaker with what the exchange showed;
4. a verb caps the body (:func:`headgate._body.read_capped`) and reads it through the call's declared responses
   (:meth:`~AsyncBaseClient._dispatch`); ``stream()`` hands the open response to the caller's block instead.

The six verbs are written out rather than generated so that each keeps an exact signature for type checkers, IDEs
and subclasses that override one; every body is a single call into :meth:`AsyncBaseClient._request`.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import functools
from dataclasses import dataclass, field, replace
from string.templatelib import Template
from typing import TYPE_CHECKING, Literal, Self, TypedDict, Unpack

import httpx2

from headgate._auth import failed_on_auth_hop
from headgate._body import read_capped, read_head
from headgate._core import (
    BREAKER_GATE,
    SILENT,
    BreakerChanged,
    CallContext,
    CallFinished,
    CallInfo,
    CallStarted,
    Emitter,
    current_timing,
    policy_is_disabled,
)
from headgate._deprecation import announce_deprecation
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
    check_timeout_fits,
    httpx_timeout,
    match_route,
    reject_upstream_httpx,
)
from headgate._request_body import check_one_body, request_content
from headgate._responses import Expect, Reader, RefusedError
from headgate._stack import Credentials, build_http_client, build_transport_stack
from headgate._url import render_path, route_template, sanitize_url
from headgate._view import KEEP, ROOT_DEFAULTS, Keep, overlay_timeout
from headgate.breaker import BreakerOpenError, CircuitBreaker
from headgate.breaker._registry import NO_BREAKER
from headgate.breaker._scope import scoped_registry
from headgate.cache._transport import host_untested, store_accepted
from headgate.errors import (
    CircuitOpenError,
    ClientError,
    ClientTimeoutError,
    DecodeError,
    ForbiddenDestinationError,
    RateLimitError,
)
from headgate.pagination import PageRequest, Paginated
from headgate.ratelimit import BudgetBacklogError, BudgetPausedError
from headgate.retry import NO_RETRY, REPLAYABLE_BODY, RetryPolicy
from headgate.retry._policy import NETWORK_ERRORS, RETRY_OUTCOME, RETRY_POLICY
from headgate.streaming import Stream

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable, Mapping
    from types import TracebackType

    from headgate._options import Route
    from headgate._request_body import Body, UploadFiles
    from headgate._responses import RefusalCheck
    from headgate.breaker import Transition, Verdict
    from headgate.breaker._registry import Breaker
    from headgate.pagination import Pager
    from headgate.ratelimit import Rate, RateBudget

__all__ = ["AsyncBaseClient", "Method", "RequestOptions"]

type Method = Literal["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"]
"""The HTTP methods a client sends: one verb each, and :meth:`AsyncBaseClient.stream`'s ``method``."""

#: A query value httpx2 encodes itself.
type _QueryValue = str | int | float | bool | None

#: How much of a refused or failed streamed response is read, for the ``ClientError``'s body snippet.
_STREAM_ERROR_HEAD_BYTES = 4096

#: Network errors that say nothing of the origin's health, so its breaker is not charged with them: a request httpx2
#: refused to write, a scheme it cannot speak, our own connection pool running out of connections, and a redirect chain
#: past ``max_redirects`` (the origin answered every hop; the loop is one URL's configuration, as a 4xx is).
_LOCAL_FAILURES = (httpx2.LocalProtocolError, httpx2.UnsupportedProtocol, httpx2.PoolTimeout, httpx2.TooManyRedirects)

#: The request options that make a body, which a page (a GET) cannot carry.
_BODY_OPTIONS = ("json", "data", "content", "files")

#: The option groups a client gets when it passes none: frozen, so one instance serves every client.
_DEFAULT_POLICY = TransportPolicy()
_DEFAULT_HTTP = HttpOptions()


class RequestOptions(TypedDict, total=False):
    """The per-call options a verb takes beyond its URL, ``expect=`` and the three explicit overrides.

    * ``params``: the query, ``Mapping[str, object]`` so that a client can pass a TypedDict (a TypedDict is assignable
      to no narrower mapping). Its values are encoded the way httpx2 encodes a mapping: a list or tuple repeats the key
      once per item, and any value that is not a ``str``, number, bool or ``None`` is sent as its ``str()``.
    * ``headers``: added to the client's; a clash is won by the call.
    * ``json`` / ``data``: a JSON body (build it with a codec flavor's ``to_builtins``) or a form body.
    * ``content``: a raw body: ``bytes``; an async iterable of ``bytes``, streamed once, so the call is never re-sent
      and a redirect that asks for the body again is not followed; or a callable returning one, called once for each
      send, so the call stays retryable. It combines with no other body option.
    * ``files``: a multipart body, as httpx2 takes it (``{"name": file}``, or ``(filename, file, content_type)``
      tuples), with ``data=`` as its form fields.
    * ``timeout``: :class:`~headgate.Timeouts` replacing the client's, or a number replacing its read timeout.
    * ``deadline``: seconds this call may take in all, or ``None`` for no deadline; absent, the view's, the route's or
      the policy's applies.
    * ``idempotency_key``: the idempotency key this call carries on every attempt, under the policy's
      ``IdempotencyKeys`` header (``Idempotency-Key`` by default).
    """

    params: Mapping[str, object] | None
    headers: Mapping[str, str]
    json: object
    data: Mapping[str, object]
    content: Body
    files: UploadFiles
    timeout: Timeouts | float
    deadline: float | None
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class _Call:
    """One call as a verb received it: what to send, and the per-call overrides."""

    method: Method
    url: str | Template
    options: RequestOptions
    retry: bool | RetryPolicy = True
    follow_redirects: bool | None = None
    cache_ttl: float | None = None


class _Report:
    """The final status of a call, for its ``CallFinished`` event: set as soon as a final response head arrives."""

    __slots__ = ("status_code",)

    def __init__(self) -> None:
        """Start with no status."""
        self.status_code: int | None = None


@dataclass(frozen=True, slots=True)
class _Prepared:
    """A built request, its call's context, whether its redirects are followed, and what its events report."""

    request: httpx2.Request
    context: CallContext
    follow_redirects: bool
    report: _Report = field(default_factory=_Report)


class AsyncBaseClient:
    """Async base for HTTP API clients. Configured through ``__init__``, which builds the transport stack once.

    **Failure policy.** Every verb takes the call's declared responses as ``expect=`` and returns what the matching
    reader read, or raises a :class:`~headgate.errors.ClientError` subtype:

    * ``TransportError`` / ``ClientTimeoutError``: a network failure or timeout, or the call's deadline passing;
    * ``CircuitOpenError``: the origin's breaker refused the call without sending it;
    * ``RateLimitError``: a 429 that survived retries, a rate budget the server paused past the call's deadline, or a
      quota refusal a reader found inside a 2xx;
    * ``UpstreamStatusError``: a 5xx that survived retries, a status the call did not declare, or a refusal carrying
      the status it arrived with (401/403 become ``AuthError``);
    * ``ForbiddenDestinationError``: a hop the SSRF guard refused (``HttpOptions(allow_private_hosts=False)``);
    * ``ResponseParseError``: a 2xx body the reader could not read;
    * ``ResponseTooLargeError``: a body past ``HttpOptions.max_response_bytes``.

    There is no ``None`` return except from a reader that returns it (:func:`~headgate._responses.nothing`): every
    undeclared non-2xx raises, a 404 included. A non-2xx the provider documents as an answer is declared with
    :meth:`Expect.on <headgate._responses.Expect.on>` and comes back as its own wire type.

    **Per-call overrides.** ``retry=False`` sends once and never re-sends (a POST or PATCH is already re-sent only
    when that is safe, see :class:`~headgate.RetryPolicy`); ``retry=RetryPolicy(...)`` replaces the client's policy for
    the call. ``follow_redirects=False`` makes a redirect raise as its 3xx. ``cache_ttl=`` sets this request's cache
    lifetime. ``timeout=``, ``deadline=`` and ``idempotency_key=`` ride :class:`RequestOptions`. :meth:`with_options`
    makes a view whose calls take other defaults for headers, timeouts, deadline and retry.

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
        rate: Rate | RateBudget,
        auth: httpx2.Auth | None = None,
        policy: TransportPolicy = _DEFAULT_POLICY,
        http: HttpOptions = _DEFAULT_HTTP,
    ) -> None:
        """Build the transport stack for *base_url*, spending from *rate*, signing requests with *auth*.

        *rate* is a :class:`~headgate.Rate`, from which the client builds a budget of its own, or a
        :class:`~headgate.RateBudget` it shares with every other client given the same one.

        *policy* configures retries, the deadline, the breaker, per-path rates, the cache, idempotency keys and
        per-route overrides (:class:`~headgate.TransportPolicy`); *http* the network transport, headers, timeouts,
        redirects, the body cap, cookies, observers and middleware (:class:`~headgate.HttpOptions`).

        :raises TypeError: for an upstream ``httpx`` *auth*.
        :raises ClientConfigurationError: for a cache partitioned by credential with an *auth* that does not declare
            its credentials (see :class:`~headgate.cache.Partition`).
        """
        reject_upstream_httpx(auth, what="auth")
        self._policy_off = policy_is_disabled()
        self._policy = policy = _effective_policy(policy, off=self._policy_off)
        self._source = type(self).__name__
        self._timing = current_timing()
        self._refusal_check = http.refusal_check
        self._max_response_bytes = http.max_response_bytes
        self._request_id_headers = http.request_id_headers
        check_timeout_fits(http.timeout, policy.deadline)
        self._timeout = http.timeout
        self._emitter = Emitter(http.observers) if http.observers else SILENT
        self._view = ROOT_DEFAULTS
        self._owns_pool = True
        credentials = Credentials.of(base_url=base_url, auth=auth, http=http)
        stack = build_transport_stack(rate, policy, http, self._timing, credentials)
        self._http_client = build_http_client(base_url=base_url, credentials=credentials, http=http, transport=stack)

    async def __aenter__(self) -> Self:
        """Return the client; leaving the block closes it (a view's block closes nothing, see :meth:`with_options`)."""
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc_value: BaseException | None, traceback: TracebackType | None
    ) -> None:
        """Close the client."""
        await self.aclose()

    async def aclose(self) -> None:
        """Close the connection pool and the transport stack under it; a cache's storage is left to whoever opened it.

        On a view (see :meth:`with_options`) it does nothing: the client the view was made from owns the pool.
        """
        if self._owns_pool:
            await self._http_client.aclose()

    def with_options(
        self,
        *,
        headers: Mapping[str, str] | None = None,
        timeout: Timeouts | float | None = None,
        deadline: float | Literal[Keep.KEEP] | None = KEEP,
        retry: bool | RetryPolicy | None = None,
    ) -> Self:
        """Return a view of this client whose calls take different defaults; the client itself is unchanged.

        * ``headers``: sent on every call of the view, over the client's; a call's ``headers=`` still wins a clash.
        * ``timeout``: replaces the client's :class:`~headgate.Timeouts` (a number replaces the read timeout only).
        * ``deadline``: seconds each call may take, or ``None`` for no deadline.
        * ``retry``: ``False`` sends once, a :class:`~headgate.RetryPolicy` replaces the client's, ``True`` restores it.

        An option left out keeps the client's (or, on a view of a view, the outer view's). A call's own options win
        over the view's, and the view's over a :class:`~headgate.Route`'s.

        The view shares everything that makes calls cooperate: the connection pool and transport stack, so the rate
        budget and the retry budget; the circuit breakers; the auth and its cached token; the observers. It is a
        shallow copy, so a subclass's own attributes are shared too. **The client it was made from owns the pool**:
        closing a view, or leaving its ``async with`` block, closes nothing, and a view stops working once that client
        is closed.

        :raises ValueError: for a timeout or deadline that is not positive.
        """
        layered = self._view.layered(headers=headers, deadline=deadline, retry=retry)
        if timeout is not None:
            check_timeout_fits(timeout, self._policy.deadline if layered.deadline is KEEP else layered.deadline)
        view = copy.copy(self)
        # A shallow copy shares the pool, the stack and any subclass state; only the view's own settings differ.
        vars(view).update(
            _view=layered,
            _owns_pool=False,
            _timeout=self._timeout if timeout is None else overlay_timeout(self._timeout, timeout),
        )
        return view

    # The verbs.

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

    # Streams and pages.

    def stream(
        self,
        method: Method,
        url: str | Template,
        *,
        retry: bool | RetryPolicy = True,
        follow_redirects: bool | None = None,
        last_event_id: str | None = None,
        **options: Unpack[RequestOptions],
    ) -> contextlib.AbstractAsyncContextManager[Stream]:
        """Send a request and open its 2xx response as a :class:`~headgate.streaming.Stream`, read as it arrives.

        ``async with client.stream("GET", "export.csv") as stream:`` then ``async for chunk in
        stream.aiter_bytes():``. The body is never buffered and never capped, and the response is closed when the block
        exits. The call runs through the same stack and failure ladder as a verb, except that:

        * a 2xx is the only success: there is no ``expect=``, and a 3xx raises unless redirects are followed;
        * the client's ``refusal_check`` runs on the response head, before the block is entered;
        * the deadline bounds opening the stream (every attempt, backoff and the head checks), not reading it, since
          the caller's pace of iteration decides how long the body takes. Each read is bounded by the read timeout;
          to bound the whole body, wrap the block in ``asyncio.timeout``;
        * a retry covers opening only; a failure while the body arrives raises ``TransportError`` or
          ``ClientTimeoutError`` from the iteration;
        * the origin's circuit breaker spans the whole block: a network failure mid-body that leaves the block counts
          against the origin, and a clean exit as a success.

        ``last_event_id=`` sends ``Last-Event-ID`` (when not empty), to resume a server-sent event stream after the
        last event a previous :class:`~headgate.streaming.Stream` saw (its ``last_event_id``).

        The request goes out when the block is entered.
        """
        if bool(last_event_id):
            options = options.copy()
            options["headers"] = {**options.get("headers", {}), "Last-Event-ID": last_event_id}
        return self._streamed(_Call(method, url, options, retry=retry, follow_redirects=follow_redirects))

    def paginate[P, I](
        self,
        url: str | Template,
        *,
        expect: Reader[P] | Expect[P],
        pages: Pager[P, I],
        retry: bool | RetryPolicy = True,
        **options: Unpack[RequestOptions],
    ) -> Paginated[P, I]:
        """Return the pages *pages* follows from the GET of *url*, each read through *expect*, iterated lazily.

        ``async for item in client.paginate(...)`` yields the items of each page in turn; ``.pages()`` yields the
        pages. Each page is a GET through the whole stack, fetched only when the iteration reaches it, and a next-page
        URL taken from a response is sent as an absolute URL, under the same credential and destination rules as any
        other. Iterating again starts again from the first page.

        :raises ValueError: for a body option (``json``, ``data``, ``content``, ``files``): pages are GETs.
        """
        if any(name in options for name in _BODY_OPTIONS):
            msg = "paginate sends GETs, so it takes no request body"
            raise ValueError(msg)
        spec: Expect[P] = expect if isinstance(expect, Expect) else Expect(expect)
        paired = Expect(_paired(spec.ok), {status: _paired(read) for status, read in spec.answers.items()})
        call_options = options.copy()
        first = PageRequest(url, call_options.pop("params", None))

        async def fetch(request: PageRequest) -> tuple[P, httpx2.Response]:
            page_options = call_options.copy()
            page_options["params"] = request.params
            return await self.get(request.url, expect=paired, retry=retry, **page_options)

        return Paginated(fetch, pages, first, source=self._source)

    # A call, end to end.

    async def _request[T](self, call: _Call, expect: Reader[T] | Expect[T]) -> T:
        """Send *call* and read the reply through its declared responses.

        Every response passes four layers, in order:

        1. **transport** (:meth:`_exchange`): a transport failure, a surviving 429/5xx, or an open breaker raises;
        2. **host**: the client's ``refusal_check`` runs on the final response;
        3. **endpoint**: a status the call declared exactly goes to its reader; any other 2xx to the success reader;
        4. **default**: any other non-2xx raises through :func:`~headgate._failures.status_error`.

        See :meth:`_dispatch` for how the last three read the body.
        """
        spec: Expect[T] = expect if isinstance(expect, Expect) else Expect(expect)
        prepared = self._prepare(call)
        for name, value in spec.headers.items():
            prepared.request.headers.setdefault(name, value)
        if spec.accept is not None:
            _stamp_accept(prepared.request, self._view.headers_for(call.options.get("headers")), spec.accept)
        async with self._reported(prepared):
            async with self._deadline(prepared), self._exchange(prepared) as (streamed, _):
                announce_deprecation(prepared.context, streamed)
                resp = await read_capped(streamed, self._max_response_bytes, self._site(prepared))
            answer = self._dispatch(resp, spec, self._site(prepared))
            # Only now that the reader accepted it may a cached miss be stored.
            await store_accepted(resp)
            return answer

    @contextlib.asynccontextmanager
    async def _streamed(self, call: _Call) -> AsyncGenerator[Stream]:
        """Open *call*'s response as a :class:`~headgate.streaming.Stream` (see :meth:`stream`).

        The exchange is entered under the deadline and left open past it, on the exit stack, so the deadline bounds the
        send and the head checks while the breaker scope and the response last until the caller's block exits.
        """
        prepared = self._prepare(call)
        async with self._reported(prepared), contextlib.AsyncExitStack() as held:
            async with self._deadline(prepared):
                streamed, verdict = await held.enter_async_context(self._exchange(prepared))
                announce_deprecation(prepared.context, streamed)
                if (refused := _refusal(self._refusal_check, streamed)) is not None:
                    head = await read_head(streamed, _STREAM_ERROR_HEAD_BYTES)
                    raise refusal_error(refused, head, self._site(prepared)) from refused
                if not streamed.is_success:
                    head = await read_head(streamed, _STREAM_ERROR_HEAD_BYTES)
                    raise status_error(head, self._site(prepared))
                # A stream has no reader: passing the head checks is what accepts it.
                await store_accepted(streamed)
            yield Stream(streamed, self._site(prepared), functools.partial(self._network_failure, prepared, verdict))

    # Preparing the request.

    def _prepare(self, call: _Call) -> _Prepared:
        """Build *call*'s request and stamp its context on it.

        Raises ``ValueError`` for a t-string value no path segment can hold, for a call that would follow redirects
        on a client with a ``refusal_check``, for a non-positive ``deadline=`` and for body options that do not
        combine; and ``ClientError`` for a URL httpx2 cannot parse.
        """
        if isinstance(call.url, Template):
            url, route = render_path(call.url), route_template(call.url)
        else:
            url, route = call.url, None
        log_url = loggable_url(url)
        follow = self._http_client.follow_redirects if call.follow_redirects is None else call.follow_redirects
        # The per-call half of the constructor's check: the client itself never follows with a refusal_check.
        if self._refusal_check is not None and follow:
            msg = f"{self._source} has a refusal_check=, so no call may follow redirects"
            raise ValueError(msg)
        options = call.options
        params = options.get("params")
        files = options.get("files")
        check_one_body(content=options.get("content"), json=options.get("json"), data=options.get("data"), files=files)
        content, replayable = request_content(options.get("content"))
        try:
            request = self._http_client.build_request(
                call.method,
                url,
                params=None if params is None else _query_pairs(params),
                headers=self._view.headers_for(options.get("headers")),
                json=options.get("json"),
                data=options.get("data"),
                content=content,
                files=files,
                timeout=httpx_timeout(self._timeout, options.get("timeout")),
            )
        except httpx2.InvalidURL as exc:
            raise _invalid_url(exc, CallSite(self._source, call.method, log_url)) from exc
        # The absolute URL is for observers only (an OpenTelemetry span's url.full and server.address).
        full_url = sanitize_url(str(request.url)) if self._emitter.active else None
        info = CallInfo(method=call.method, url=log_url, route=route, full_url=full_url)
        route_policy = match_route(self._policy.routes, request.url.path)
        deadline = self._deadline_of(call, route_policy)
        if (timeout := options.get("timeout")) is not None:
            check_timeout_fits(timeout, deadline)
        context = CallContext(info, clock=self._timing.clock, deadline_after=deadline, emitter=self._emitter)
        context.follow_redirects = follow
        if (retry := self._retry_of(call, route_policy)) is not None:
            context.put(RETRY_POLICY, retry)
        if not replayable:
            context.put(REPLAYABLE_BODY, replayable)
        self._stamp_idempotency_key(request, context, options.get("idempotency_key"))
        # The cache transport only setdefaults the client's own TTL, so a per-call TTL stamped here wins.
        if call.cache_ttl is not None:
            request.extensions["hishel_ttl"] = call.cache_ttl
        context.attach(request)
        return _Prepared(request, context, follow)

    def _deadline_of(self, call: _Call, route: Route | None) -> float | None:
        """Return the seconds *call* may take: its own ``deadline=``, else the view's, its route's, the policy's."""
        if "deadline" in call.options:
            deadline = call.options["deadline"]
            if deadline is not None and not deadline > 0:
                msg = f"deadline must be > 0, or None (got {deadline})"
                raise ValueError(msg)
            return deadline
        if self._view.deadline is not KEEP:
            return self._view.deadline
        if route is not None and route.deadline is not None:
            return route.deadline
        return self._policy.deadline

    def _retry_of(self, call: _Call, route: Route | None) -> RetryPolicy | None:
        """Return *call*'s own retry policy when it overrides the client's: per call, view or route; else ``None``."""
        if self._policy_off or call.retry is False:
            return NO_RETRY
        if isinstance(call.retry, RetryPolicy):
            return call.retry
        if self._view.retry is not None:
            return self._view.retry
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

    # Around the exchange: events and the deadline.

    @contextlib.asynccontextmanager
    async def _reported(self, prepared: _Prepared) -> AsyncGenerator[None]:
        """Emit ``CallStarted`` and, on the way out, ``CallFinished``; add the call's notes to a ``ClientError``."""
        context = prepared.context
        if context.observed:
            context.emit(CallStarted(call=context.info))
        error: str | None = None
        try:
            yield
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
                        status_code=prepared.report.status_code,
                        error=error,
                        attempts=context.attempt,
                        elapsed=context.elapsed(),
                    )
                )

    @contextlib.asynccontextmanager
    async def _deadline(self, prepared: _Prepared) -> AsyncGenerator[None]:
        """Bound the block by the call's deadline, raising ``ClientTimeoutError`` when it passes.

        The deadline is absolute on the client's clock; the block is cancelled after the seconds that remained when it
        began, so everything in it (rate-budget waits, every attempt and backoff, auth hops, the body read) counts.
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
            context = prepared.context
            if context.progress.dispatched or context.attempt == 0:
                site, unsent = self._site(prepared), ""
            else:
                site, unsent = self._unsent_site(prepared), f", before attempt {context.attempt} was sent"
            msg = f"{site.method} {site.url}: deadline exceeded after {context.elapsed():.3g}s{unsent}"
            raise ClientTimeoutError(msg, **site.details()) from None

    # The exchange: one send under the origin's breaker.

    @contextlib.asynccontextmanager
    async def _exchange(self, prepared: _Prepared) -> AsyncGenerator[tuple[httpx2.Response, Verdict]]:
        """Send the request under its origin's circuit breaker; yield the streamed response, and the breaker's verdict.

        The response is closed afterwards. The verdict lets a reader of the body outside this scope charge a network
        failure to the origin (:meth:`_network_failure`).

        The response yielded is anything but a 429 or 5xx: one of those means the retry transport ran out of
        attempts, and it is raised here, so no call can declare either as an answer. A network failure or timeout,
        in the send or in the caller's body read inside the block, becomes ``TransportError`` /
        ``ClientTimeoutError``; an open breaker ``CircuitOpenError``; a rate budget the server paused past the
        call's deadline ``RateLimitError``; and a hop the SSRF guard refused ``ForbiddenDestinationError``.

        The breaker is driven here rather than by a transport layer because this is the only scope that spans both the
        send and the body read. It is keyed on the origin the request is addressed to, so a followed redirect's hops
        are charged to that origin; what it is charged with is decided in :meth:`_sent`. Every transition the call
        causes is emitted as ``BreakerChanged``, and the call's context carries :data:`~headgate._core.BREAKER_GATE`,
        so the retry layer stops re-sending once the breaker refuses.
        """
        context = prepared.context
        breaker = self._breaker_for(prepared.request.url)
        try:
            with breaker.track(self._breaker_listener(breaker, context)) as verdict:
                context.put(BREAKER_GATE, _breaker_gate(breaker, verdict))
                async with self._sent(prepared, verdict) as streamed:
                    yield streamed, verdict
        except BreakerOpenError as exc:
            details = self._site(prepared).details(retry_after=exc.retry_after)
            raise CircuitOpenError(str(exc), **details) from None
        except BudgetBacklogError as exc:
            # Our own pacing, not the provider's: the call was never sent (the retry layer withdrew the attempt), and
            # sending it later will be admitted.
            site = self._site(prepared)
            details = site.details(retry_after=exc.retry_after)
            details["retryable"] = True
            msg = f"{site.method} {site.url}: never sent: {exc}"
            raise ClientTimeoutError(msg, **details) from None
        except BudgetPausedError as exc:
            # Not the retry policy's call: the budget says when sending again will be admitted.
            details = self._site(prepared).details(retry_after=exc.retry_after)
            details["retryable"] = True
            raise RateLimitError(str(exc), status_code=exc.status_code, **details) from None
        except httpx2.InvalidURL as exc:
            # A redirect joined past httpx2's length limit. Not an HTTPError, and not an origin failure.
            raise _invalid_url(exc, self._site(prepared)) from exc
        except ForbiddenDestinationError as exc:
            # The SSRF guard refused a hop before it was sent; it names the destination, the site names the call.
            site = self._site(prepared)
            msg = f"{site.method} {site.url}: {exc}"
            raise ForbiddenDestinationError(msg, **site.details()) from None

    @contextlib.asynccontextmanager
    async def _sent(self, prepared: _Prepared, verdict: Verdict) -> AsyncGenerator[httpx2.Response]:
        """Send the request and yield its streamed response, marking *verdict* with what the exchange showed.

        The final status is recorded for ``CallFinished`` as soon as the head arrives. A surviving 429/5xx is raised
        with a bounded head of its body, read here while the response is still a stream. What the origin's breaker is
        charged with:

        * **a failure**: a 5xx this request fetched, whether or not the retry policy re-sends it (a 501 is the origin
          failing to serve the request as surely as a 503), and a network failure or timeout;
        * **nothing**: a 429 (the origin is up and asked us to slow down, which the rate budget handles); a network
          error of our own side's making, or a redirect loop (:data:`_LOCAL_FAILURES`, and an invalid URL); a failure
          on a hop an auth flow made to mint a credential (that is the credential host's failure); and an outcome
          another request's attempt produced (a cache hit, a stored 5xx served from the cache, or a download or failure
          the cache shared from an identical GET, :data:`~headgate.cache._transport.HOST_UNTESTED`), since only the
          request that fetched tested the origin.
        """
        request = prepared.request
        try:
            streamed = await self._http_client.send(request, stream=True, follow_redirects=prepared.follow_redirects)
            prepared.report.status_code = streamed.status_code
            try:
                _charge_response(streamed, verdict)
                if is_transient_status(streamed.status_code):
                    head = await read_head(streamed, _STREAM_ERROR_HEAD_BYTES)
                    raise transient_status_error(head, self._site(prepared))
                yield streamed
            finally:
                # Closing a partly read compressed stream can raise DecodingError; it must not mask the error in flight.
                with contextlib.suppress(httpx2.HTTPError):
                    await streamed.aclose()
        except NETWORK_ERRORS as exc:
            error = self._network_failure(prepared, verdict, exc)
            if isinstance(exc, httpx2.LocalProtocolError):
                # Its message, and so its traceback, quotes the header value it refused, usually the credential.
                raise error from None
            raise error from exc

    def _network_failure(self, prepared: _Prepared, verdict: Verdict, exc: httpx2.HTTPError | OSError) -> ClientError:
        """Charge the network failure *exc* to the origin on *verdict*, and return the ``ClientError`` it becomes."""
        if _network_failure_charges_origin(exc):
            verdict.mark_failed()
        return transport_error(exc, self._site(prepared))

    def _breaker_for(self, url: httpx2.URL) -> Breaker:
        """Return *url*'s origin's breaker from the policy's registry, or a no-op one when the client has no breaker."""
        policy = self._policy.breaker
        if policy is None:
            return NO_BREAKER
        return policy.registry.breaker_for(
            url, failure_threshold=policy.failure_threshold, recovery_timeout=policy.recovery_timeout
        )

    @staticmethod
    def _breaker_listener(breaker: Breaker, context: CallContext) -> Callable[[Transition], None] | None:
        """Return what emits *breaker*'s transitions as ``BreakerChanged`` events, or ``None`` when nobody listens."""
        if not context.observed or not isinstance(breaker, CircuitBreaker):
            return None

        def emit(transition: Transition) -> None:
            context.emit(
                BreakerChanged(
                    origin=breaker.name,
                    previous=transition.previous,
                    state=transition.state,
                    failures=transition.failures,
                    call=context.info,
                )
            )

        return emit

    # Reading the answer.

    def _dispatch[T](self, resp: httpx2.Response, spec: Expect[T], site: CallSite) -> T:
        """Read the final *resp* through the host check, the call's declared responses and the status ladder.

        A ``RefusedError`` from a reader, or raised or returned by the ``refusal_check``, becomes the ``ClientError``
        its kind names, with the real status. A reader that finds the body is not the shape it expected (a codec's
        ``DecodeError``, or the ``ValueError``, ``LookupError`` or ``TypeError`` hand-written code over
        ``response.json()`` raises) on a 2xx raises ``ResponseParseError`` naming the status and request line; on a
        declared non-2xx it falls through to the status ladder, because a declaration covers only the body shape it
        names. A body whose ``Content-Type`` is not the one ``Expect(accept=...)`` asked for is such a shape error.
        """
        if (refused := _refusal(self._refusal_check, resp)) is not None:
            raise refusal_error(refused, resp, site) from refused
        read = spec.answers.get(resp.status_code)
        if read is None:
            if not resp.is_success:
                raise status_error(resp, site)
            read = spec.ok
        try:
            if not spec.accepts(resp):
                content_type = resp.headers.get("content-type") or "no Content-Type"
                msg = f"expected {spec.accept}, got {content_type}"
                raise DecodeError(msg)
            return read(resp)
        # The one place a reader's RefusedError is caught: it becomes the ClientError carrying the status the wire sent.
        except RefusedError as refused:
            raise refusal_error(refused, resp, site) from refused
        except SHAPE_ERRORS as exc:
            if resp.is_success:
                raise parse_error(exc, resp, site) from exc
            raise status_error(resp, site) from exc

    def _site(self, prepared: _Prepared) -> CallSite:
        """Return where *prepared*'s call is now, for an error raised about it: the attempt count is read now."""
        context = prepared.context
        info = context.info
        return CallSite(
            self._source, info.method, info.url, context.attempt, self._request_id_headers, context.get(RETRY_OUTCOME)
        )

    def _unsent_site(self, prepared: _Prepared) -> CallSite:
        """Return :meth:`_site` for a call whose current attempt never reached the network: it does not count."""
        return replace(self._site(prepared), attempts=prepared.context.attempt - 1)


def _effective_policy(policy: TransportPolicy, *, off: bool) -> TransportPolicy:
    """Return the policy a client built now runs: *policy*, less retries and the breaker when transport policy is *off*.

    The breaker's registry is the one a :func:`~headgate.testing.isolated_breakers` block substitutes for the default,
    if any. The rate budget checks for transport policy being off itself, when it is built.
    """
    if off:
        return replace(policy, retry=NO_RETRY, breaker=None)
    breaker = policy.breaker
    if breaker is None or (registry := scoped_registry(breaker.registry)) is breaker.registry:
        return policy
    return replace(policy, breaker=replace(breaker, registry=registry))


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


def _stamp_accept(request: httpx2.Request, explicit: Mapping[str, str] | None, accept: str) -> None:
    """Send *accept* as *request*'s ``Accept``, over the client's default but never over an *explicit* one.

    *explicit* is what the call and its view set in ``headers=``.
    """
    if explicit is None or "accept" not in {name.lower() for name in explicit}:
        request.headers["Accept"] = accept


def _charge_response(response: httpx2.Response, verdict: Verdict) -> None:
    """Mark *verdict* with what *response* says about its origin (see :meth:`AsyncBaseClient._sent`).

    A response another request's attempt produced (a cache hit, a stored response, a shared download) is untested; a
    5xx this request fetched itself is a failure; a 429 is neither, since the origin is up and asked us to slow down.
    """
    if host_untested(response) or response.extensions.get("hishel_from_cache"):
        verdict.mark_untested()
    elif response.status_code >= httpx2.codes.INTERNAL_SERVER_ERROR:
        verdict.mark_failed()


def _network_failure_charges_origin(exc: Exception) -> bool:
    """Return whether a network failure is the origin's, to charge its breaker with (see :meth:`AsyncBaseClient._sent`).

    Not when it is of our own side's making or a redirect loop (:data:`_LOCAL_FAILURES`), happened on a hop an auth
    flow made to mint a credential, or was shared by the cache from another request's attempt.
    """
    return not (isinstance(exc, _LOCAL_FAILURES) or failed_on_auth_hop(exc) or host_untested(exc))


def _breaker_gate(breaker: Breaker, verdict: Verdict) -> Callable[[], bool]:
    """Return the call's :data:`~headgate._core.BREAKER_GATE`: whether its origin's breaker refuses now.

    The half-open breaker's probe is never refused by its own probe slot, so the probe keeps its retries and its whole
    retry sequence is the one recovery test.
    """

    def refusing() -> bool:
        return not verdict.probe and breaker.refusing

    return refusing


def _refusal(check: RefusalCheck | None, resp: httpx2.Response) -> RefusedError | None:
    """Run the client's *check* on *resp*, treating a ``RefusedError`` it raises like one it returns."""
    if check is None:
        return None
    try:
        return check(resp)
    except RefusedError as refused:
        return refused


def _paired[T](read: Reader[T]) -> Reader[tuple[T, httpx2.Response]]:
    """Return a reader that returns what *read* reads together with the response, for a pager to read headers from."""

    def _read(resp: httpx2.Response, /) -> tuple[T, httpx2.Response]:
        return read(resp), resp

    return _read


def _invalid_url(exc: httpx2.InvalidURL, site: CallSite) -> ClientError:
    """Build the ``ClientError`` for a URL httpx2 rejected, naming it and httpx2's reason, both bounded."""
    return ClientError(f"invalid URL {site.url}: {shorten(str(exc))}", **site.details())
