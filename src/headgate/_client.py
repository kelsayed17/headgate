# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""The client: :class:`Client`, a policy layer over ``httpx2.AsyncClient`` that an SDK holds.

httpx2 does the URL joining, headers, timeouts and connection pooling. This adds retries, rate limiting, a per-origin
circuit breaker, a deadline, a response-size cap, events, and the mapping of every failure onto the ``ClientError``
tree. It orchestrates; each policy lives in its own module, and the transport stack under ``httpx2.AsyncClient`` is
assembled in :mod:`headgate._stack`.

Every call (a verb, :meth:`~Client.stream`, each page of :meth:`~Client.paginate`) is packed into a
:class:`_Call` and runs through the same steps:

1. :meth:`~Client._prepare` renders the URL, builds the ``httpx2.Request`` once, and stamps the call's
   :class:`~headgate._core.CallContext` on it: method, sanitized URL, template, deadline, retry override, body
   replayability, idempotency key and redirect choice;
2. :meth:`~Client._reported` emits ``CallStarted`` and ``CallFinished`` around the call and adds its notes to
   the error it raises; :meth:`~Client._deadline` bounds it by its deadline;
3. :meth:`~Client._exchange` sends the request under the origin's circuit breaker, through the transport
   stack, and :meth:`~Client._sent` maps a network failure or a surviving 429/5xx onto a ``ClientError``
   (:mod:`headgate._failures`), charging the breaker with what the exchange showed;
4. a verb caps the body (:func:`headgate._body.read_capped`) and reads it through the call's declared responses
   (:meth:`~Client._dispatch`); ``stream()`` hands the open response to the caller's block instead.

The six verbs are written out rather than generated so that each keeps an exact signature for type checkers and IDEs;
every body is a single call into :meth:`Client._request`.
"""

import asyncio
import contextlib
import copy
import functools
import math
from collections.abc import AsyncGenerator, Callable, Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from string.templatelib import Template
from types import TracebackType
from typing import Final, Literal, Self, Unpack, cast, final, overload

import httpx2

from headgate._auth import MINTING, Auth, check_timing, failed_on_auth_hop, hold_timing, mint_hop_failure, was_checked
from headgate._body import read_capped, read_head
from headgate._call_options import (
    BODY_HINTS,
    CALL_KEYS,
    REQUEST_KEYS,
    STREAM_KEYS,
    CallOptions,
    RequestOptions,
    StreamOptions,
    check_overrides,
)
from headgate._core import (
    BREAKER_GATE,
    EXPLICIT_HEADERS,
    SILENT,
    BudgetBacklogError,
    CallContext,
    Emitter,
    ExplicitHeaders,
    Observer,
    Slot,
    Timing,
    check_kind,
    check_options,
    named,
    policy_is_disabled,
    url_path,
)
from headgate._deprecation import announce_deprecation
from headgate._errors import ClientError, ClientTimeoutError, RateLimitError
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
from headgate._middleware import Middleware
from headgate._options import (
    KEYED_METHODS,
    CallPolicy,
    HttpOptions,
    Route,
    RouteOptions,
    Timeouts,
    check_deadline,
    check_hooks,
    check_middleware,
    check_observers,
    check_timeout_fits,
    httpx_timeout,
    match_route,
    reject_upstream_httpx,
)
from headgate._request_body import Json, Lowered, WireValue, Writer, lowered, wire_value
from headgate._responses import Expect, Reader, ResponseCheck, accepts, with_answers
from headgate._stack import Credentials, build_http_client, build_transport_stack
from headgate._url import CallUrl, render_path, route_template, sanitize_url
from headgate._view import KEEP, ROOT_DEFAULTS, VIEW_KEYS, ViewOptions
from headgate.breaker import BreakerRegistry, CircuitBreaker
from headgate.breaker._breaker import BreakerOpenError, Transition, Verdict
from headgate.breaker._registry import NO_BREAKER, Breaker
from headgate.cache._policy import HttpCache
from headgate.cache._transport import (
    HAS_BODY,
    MAX_RESPONSE_BYTES,
    STREAMED,
    UNCACHED,
    host_untested,
    served_from_cache,
    stamp_ttl,
    store_accepted,
)
from headgate.errors import CircuitOpenError, DecodeError, ForbiddenDestinationError, RefusedError
from headgate.events import BreakerChanged, CallFinished, CallInfo, CallStarted
from headgate.pagination import Pager, PageRequest, WritingPager
from headgate.pagination._paginated import PageWalk, Paginated, check_page_budget
from headgate.ratelimit._budget import BudgetPausedError, RateBudget
from headgate.ratelimit._budgets import CALL_BUDGET, Lane, PathBudgets
from headgate.ratelimit._rate import Rate
from headgate.retry._policy import NETWORK_ERRORS, REPLAYABLE_BODY, RETRY_OUTCOME, RETRY_POLICY, SEND_ONCE, RetryPolicy
from headgate.streaming import Stream
from headgate.streaming._stream import OpenStream
from headgate.timing import current_timing, timing_override

__all__ = ["Built", "Client", "Method", "built_with"]

type Method = Literal["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "QUERY"]
"""The HTTP methods a client sends: one verb each, and :meth:`Client.stream`'s ``method``."""

#: How much of a refused or failed streamed response is read, for the ``ClientError``'s body snippet.
_STREAM_ERROR_HEAD_BYTES = 4096

#: The media type ``events()`` asks for.
_EVENT_STREAM: Final = "text/event-stream"

#: Network errors that say nothing of the origin's health, so its breaker is not charged with them: a request httpx2
#: refused to write, a scheme it cannot speak, our own connection pool running out of connections, and a redirect chain
#: past ``max_redirects`` (the origin answered every hop; the loop is one URL's configuration, as a 4xx is).
_LOCAL_FAILURES = (httpx2.LocalProtocolError, httpx2.UnsupportedProtocol, httpx2.PoolTimeout, httpx2.TooManyRedirects)

#: The running timer of the call's deadline, put on its context by ``Client._deadline``: whether it fired is
#: what tells the deadline cutting an attempt off from the caller's task being cancelled.
_DEADLINE_TIMER = Slot[asyncio.Timeout]("deadline_timer")

#: The attribute marking a ``ClientError`` a call has reported, so an enclosing stream leaves it as that call's.
_REPORTED = "headgate.reported"

#: The origin a walk's first page was sent to, set while ``paginate`` fetches a later page, whose URL the server chose:
#: the call's own ``headers=`` reach that origin and trusted ones only (see :class:`~headgate._core.ExplicitHeaders`).
_WALK_ORIGIN = ContextVar[httpx2.Origin | None]("headgate_walk_origin", default=None)

#: What ``get()`` and ``head()`` say of a body keyword: neither method takes one, and ``query()`` is the read that does.
_NO_BODY_HINTS: Final = {
    method: dict.fromkeys(
        ("body", *BODY_HINTS),
        f"a {method} has no body, and the cache does not key one: use query(), the safe read with a body",
    )
    for method in ("GET", "HEAD")
}

#: What ``paginate`` says of a body keyword: a walk's body is the raw value its pager rewrites, ``payload=``.
_WALK_BODY = "a walk's body is payload=, the value its pager rewrites for each page"
_WALK_HINTS = dict.fromkeys(("body", "json", "data", "content", "files", "query"), _WALK_BODY)

#: Why a walk of POSTs refuses one fixed idempotency key.
_ONE_KEY_FOR_EVERY_PAGE = "an idempotency key would be sent with every page's body; a walk of POSTs takes none"

#: The option groups a client gets when it passes none: frozen, so one instance serves every client.
_DEFAULT_POLICY = CallPolicy()
_DEFAULT_HTTP = HttpOptions()


@dataclass(frozen=True, slots=True)
class _Call:
    """One call as a verb received it: what to send, and its options."""

    method: Method
    url: CallUrl
    options: CallOptions | RequestOptions | StreamOptions


class _BlockError(Exception):
    """An exception a ``stream()`` block raised, carried past the exchange's failure mapping to be raised unchanged."""

    def __init__(self, error: Exception) -> None:
        """Carry *error*."""
        super().__init__()
        self.error = error


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


@dataclass(frozen=True, slots=True)
class Built:
    """What a client was built with, for :mod:`headgate.testing` to read back: its cache, budgets, pool and breakers."""

    cache: HttpCache | None
    budgets: PathBudgets
    http: httpx2.AsyncClient
    breakers: BreakerRegistry | None


#: The key a client keeps its :class:`Built` under, in its ``__dict__``, which a view (a shallow copy) shares.
_BUILT = "headgate.built"


def built_with(client: Client) -> Built:
    """Return what *client* (or the client a view was made from) was built with.

    :raises TypeError: for anything but a :class:`Client`, such as the SDK that holds one.
    """
    if not isinstance(client, Client):
        msg = f"{type(client).__name__} is not a Client: pass the Client it holds"
        raise TypeError(msg)
    return cast("Built", vars(client)[_BUILT])


@final
class Client:
    """An HTTP API client, configured through ``__init__``, which builds the transport stack once.

    An SDK holds one rather than inheriting from it (``self._http = Client(...)``), and calls its verbs; subclassing
    it raises ``TypeError``. Every :class:`~headgate.ClientError` it raises names its ``base_url``'s host as
    its ``source``.

    **Failure policy.** Every verb takes the call's declared responses as ``expect=`` and returns what the matching
    reader read, or raises a :class:`~headgate.ClientError` subtype:

    * ``TransportError`` / ``ClientTimeoutError``: a network failure or timeout, or the call's deadline passing;
    * ``CircuitOpenError``: the origin's breaker refused the call without sending it;
    * ``RateLimitError``: a 429 that survived retries, a rate budget the server paused past the call's deadline, or a
      quota refusal a reader found inside a 2xx;
    * ``UpstreamStatusError``: a 5xx that survived retries, a status the call did not declare, or a refusal carrying
      the status it arrived with (401/403 become ``AuthError``);
    * ``ForbiddenDestinationError``: a hop the SSRF guard refused (``SecurityOptions(block_private_hosts=True)``), or
      a token mint's hop redirected off its origin;
    * ``ResponseParseError``: a 2xx body the reader could not read;
    * ``ResponseTooLargeError``: a body past the call's ``max_response_bytes``, else ``HttpOptions.max_response_bytes``.

    There is no ``None`` return except from a reader that returns it (:data:`~headgate._responses.nothing`): every
    undeclared non-2xx raises, a 404 included. A non-2xx the provider documents as an answer is declared with
    :meth:`Expect.on <headgate._responses.Expect.on>` and comes back as its own wire type.

    **Per-call options** are :class:`~headgate.RequestOptions`. An option left out takes the view's, the route's or the
    client's, and ``None`` is off: ``retry=None`` sends once and never re-sends (a POST or PATCH is already re-sent only
    when that is safe, see :class:`~headgate.RetryPolicy`), and ``retry=RetryPolicy(...)`` replaces the policy for the
    call. ``follow_redirects=False`` makes a redirect raise as its 3xx. ``cache_ttl=`` sets this response's cache
    lifetime, ``None`` for none. A keyword no option names raises ``TypeError`` before anything is sent.
    :meth:`with_options` makes a view whose calls take other defaults for headers, timeouts, deadline, retry and
    redirects.

    **URLs.** A t-string URL (``t"series/{series_id}"``) is rendered by :func:`headgate._url.render_path`, which escapes
    every interpolation as one path segment and raises ``ValueError`` for a value no segment can hold (as
    :func:`~headgate._url.escape_segment` does): that is a programming error at the call site. Its template is the
    call's ``CallInfo.template`` in events. An absolute ``httpx2.URL`` is passed through verbatim, so a paginator can
    forward a cursor URL from a response body; a literal ``str`` is passed through the same way. A relative
    ``httpx2.URL`` raises ``TypeError``, since its only use would be to skip escaping, and so does a URL of any other
    kind. A computed ``str``, such as an f-string, does not type-check, because it skipped escaping. A URL httpx2 cannot
    parse raises the root ``ClientError``, since such a URL often comes from the wire rather than the code; a redirect
    to one is the host's failure, a ``TransportError``. Every message names a URL redacted and cut to a few hundred
    characters.

    **Time.** The client takes its timing from :func:`headgate.timing.current_timing` when it is built.
    """

    def __init__(
        self,
        *,
        base_url: str,
        auth: Auth | None = None,
        policy: CallPolicy = _DEFAULT_POLICY,
        http: HttpOptions = _DEFAULT_HTTP,
        observers: Sequence[Observer] = (),
        middleware: Sequence[Middleware] = (),
    ) -> None:
        """Build the transport stack for *base_url*, signing requests with *auth*.

        *policy* configures the rate, retries, the timeouts and the deadline, the breaker, the cache, idempotency keys
        and per-route overrides (:class:`~headgate.CallPolicy`); *http* the network transport,
        headers, redirects, the body cap and cookies (:class:`~headgate.HttpOptions`).

        *observers* receive the client's events (see :mod:`headgate.events`), and *middleware* runs once per attempt,
        in order, inside the retry layer (see :class:`~headgate.Middleware`).

        :raises TypeError: for an argument of the wrong kind: an *auth* that is not a :class:`~headgate.Auth` (a plain
            ``httpx2.Auth`` declares nothing it stamps) or is an upstream ``httpx`` one, *observers* or *middleware*
            that are not a sequence, a middleware that does not declare its credentials or is not async, an observer
            whose ``notify`` is, and a *base_url*, *policy* or *http* that is not the kind it names.
        """
        check_kind("base_url", base_url, str, "the API root, such as 'https://api.example.com/v1'")
        check_kind("policy", policy, CallPolicy, "CallPolicy(...)")
        check_kind("http", http, HttpOptions, "HttpOptions(...)")
        reject_upstream_httpx(auth, what="auth")
        _check_auth(auth)
        observers, middleware = _stack_parts("observers", observers), _stack_parts("middleware", middleware)
        check_middleware(middleware)
        check_observers(observers)
        self._policy_off = policy_is_disabled()
        self._policy = policy = _effective_policy(policy, off=self._policy_off)
        self._source = httpx2.URL(base_url).netloc.decode("ascii")  # host, and a port other than the default
        self._timing = current_timing()
        check_timing(auth, self._timing)
        self._breakers = _registry_of(policy, self._timing)
        self._response_check = http.response_check
        self._max_response_bytes = http.max_response_bytes
        self._request_id_headers = http.request_id_headers
        self._timeout = policy.timeout
        self._view_timeout: Timeouts | None = None
        self._emitter = Emitter(observers) if observers else SILENT
        self._view = ROOT_DEFAULTS
        self._owns_pool = True
        credentials = Credentials.of(base_url=base_url, auth=auth, http=http, middleware=middleware)
        lanes = [Lane(route.fragment, *_route_rate(route)) for route in policy.routes]
        self._budgets = budgets = PathBudgets(
            default=policy.rate, lanes=lanes, timing=self._timing, origin=httpx2.URL(base_url).origin
        )
        stack = build_transport_stack(budgets, policy, http, self._timing, credentials, observed=bool(observers))
        self._http_client = build_http_client(
            base_url=base_url, credentials=credentials, http=http, timeout=policy.timeout, transport=stack
        )
        vars(self)[_BUILT] = Built(cache=policy.cache, budgets=budgets, http=self._http_client, breakers=self._breakers)
        # Last, so a client refused above leaves the auth's clock unfixed; check_timing refused a clash before.
        hold_timing(auth, self._timing)

    def __init_subclass__(cls, **kwargs: object) -> None:
        """Refuse a subclass: an SDK holds a ``Client`` and calls it."""
        del kwargs
        msg = (
            f"{cls.__name__} cannot subclass Client: hold one instead, as self._http = Client(base_url=...), and call "
            "self._http.get(...)"
        )
        raise TypeError(msg)

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

    def with_options(self, **options: Unpack[ViewOptions]) -> Self:
        """Return a view of this client whose calls take different defaults; the client itself is unchanged.

        The options are :class:`~headgate.ViewOptions`: ``headers=``, ``timeout=``, ``deadline=``, ``retry=`` and
        ``follow_redirects=``. An option left out keeps the client's (or, on a view of a view, the outer view's). A
        call's own options win over the view's, and the view's over a :class:`~headgate.Route`'s.

        The view shares everything that makes calls cooperate: the connection pool and transport stack, so the rate
        budget and the retry budget; the circuit breakers; the auth and its cached token; the observers. **The client it
        was made from owns the pool**:
        closing a view, or leaving its ``async with`` block, closes nothing, and a view stops working once that client
        is closed.

        :raises ValueError: for a deadline that is not positive and finite, and for a timeout longer than the deadline
            the view's calls run under, since it could never take effect. A deadline shorter than the timeouts is
            accepted: cutting each attempt's timeouts to the time left is what a short deadline is for. Also for
            ``follow_redirects=True`` on a client with a ``response_check``, which no call may follow under.
        :raises TypeError: for a keyword :class:`~headgate.ViewOptions` does not name, and for a ``retry`` or
            ``follow_redirects`` of the wrong kind.
        """
        check_options("with_options", options, VIEW_KEYS)
        layered = self._view.layered(options)
        if self._response_check is not None and layered.follow_redirects is True:
            msg = f"the client of {self._source} has a response_check=, so no view may follow redirects"
            raise ValueError(msg)
        if (timeout := options.get("timeout")) is not None:
            check_timeout_fits(timeout, self._policy.deadline if layered.deadline is KEEP else layered.deadline)
        view = copy.copy(self)
        # A shallow copy shares the pool and the stack; only the view's own settings differ.
        vars(view).update(
            _view=layered,
            _owns_pool=False,
            _timeout=self._timeout if timeout is None else timeout.over(self._timeout),
            _view_timeout=_layered_timeout(timeout, self._view_timeout),
        )
        return view

    # The verbs.

    async def get[T](
        self,
        url: CallUrl,
        *,
        expect: Reader[T] | Expect[T],
        **options: Unpack[CallOptions],
    ) -> T:
        """Send a GET and read the reply through *expect*. See :class:`Client`.

        A GET takes no ``body=``: it has no meaning there, and the cache keys an entry by its URL alone. A read with a
        body is :meth:`query`.
        """
        call = _Call("GET", url, options)
        return await self._request(call, expect)

    async def delete[T](
        self,
        url: CallUrl,
        *,
        expect: Reader[T] | Expect[T],
        **options: Unpack[RequestOptions],
    ) -> T:
        """Send a DELETE and read the reply through *expect*. See :class:`Client`."""
        call = _Call("DELETE", url, options)
        return await self._request(call, expect)

    async def post[T](
        self,
        url: CallUrl,
        *,
        expect: Reader[T] | Expect[T],
        **options: Unpack[RequestOptions],
    ) -> T:
        """Send a POST and read the reply through *expect*. See :class:`Client`."""
        call = _Call("POST", url, options)
        return await self._request(call, expect)

    async def query[T](
        self,
        url: CallUrl,
        *,
        expect: Reader[T] | Expect[T],
        **options: Unpack[RequestOptions],
    ) -> T:
        """Send a QUERY, the safe and idempotent method that carries a body, and read the reply through *expect*.

        It takes a ``body=`` as :meth:`post` does. Being idempotent, it is re-sent after any retryable failure and may
        be hedged, as a GET is; it is never cached. See :class:`Client`.
        """
        call = _Call("QUERY", url, options)
        return await self._request(call, expect)

    async def put[T](
        self,
        url: CallUrl,
        *,
        expect: Reader[T] | Expect[T],
        **options: Unpack[RequestOptions],
    ) -> T:
        """Send a PUT and read the reply through *expect*. See :class:`Client`."""
        call = _Call("PUT", url, options)
        return await self._request(call, expect)

    async def patch[T](
        self,
        url: CallUrl,
        *,
        expect: Reader[T] | Expect[T],
        **options: Unpack[RequestOptions],
    ) -> T:
        """Send a PATCH and read the reply through *expect*. See :class:`Client`."""
        call = _Call("PATCH", url, options)
        return await self._request(call, expect)

    async def head[T](
        self,
        url: CallUrl,
        *,
        expect: Reader[T] | Expect[T],
        **options: Unpack[CallOptions],
    ) -> T:
        """Send a HEAD and read the reply through *expect*: :data:`~headgate.nothing` for a HEAD read for its status.

        A reader still has the status line and headers, ``Content-Length`` and ``Content-Encoding`` included, so
        ``expect=`` reads a header off a HEAD; the size cap is not applied to a body that does not exist. It takes no
        ``body=``, as :meth:`get` takes none.
        """
        call = _Call("HEAD", url, options)
        return await self._request(call, expect)

    # Streams and pages.

    def stream(
        self,
        url: CallUrl,
        *,
        method: Method = "GET",
        **options: Unpack[StreamOptions],
    ) -> contextlib.AbstractAsyncContextManager[Stream]:
        """Send a request, a GET unless *method* says otherwise, and open its 2xx response as a ``Stream``.

        ``async with client.stream("export.csv") as stream:`` then ``async for chunk in
        stream.aiter_bytes():``. The body is never buffered and never capped, and the response is closed when the block
        exits. The call runs through the same stack and failure ladder as a verb, except that:

        * a 2xx is the only success: there is no ``expect=``, and a 3xx raises unless redirects are followed;
        * the client's ``response_check`` runs on the response head, before the block is entered;
        * the deadline bounds opening the stream (every attempt, backoff and the head checks), not reading it, since
          the caller's pace of iteration decides how long the body takes. Once the head has arrived, only the read
          timeout bounds each read, so a call's read timeout may be longer than its deadline; to bound the whole
          stream, wrap the block in ``asyncio.timeout``;
        * a retry covers opening only; a failure while the body arrives raises ``TransportError`` or
          ``ClientTimeoutError`` from the iteration;
        * the origin's circuit breaker is told of the answer at the response head, before the block is entered, so a
          stream that took the half-open probe closes the breaker then; a network failure mid-body counts against the
          origin as one more failure, even when the block catches it;
        * an exception the block raises that is not a ``ClientError`` is the caller's own, even an ``OSError`` or an
          httpx2 error: it propagates unchanged, the breaker is charged nothing for it, and ``CallFinished`` reports
          the status the stream opened with and no error.

        A GET or HEAD stream takes no ``body=``, as :meth:`get` takes none: ``method="QUERY"`` is the read with a body.

        A server-sent event stream is :meth:`events`, which also resumes one after an event.

        The request goes out when the block is entered.
        """
        check_options("stream", options, STREAM_KEYS, BODY_HINTS)
        check_overrides(options)
        if method in _NO_BODY_HINTS and options.get("body") is not None:
            msg = (
                f"stream() takes no body= for a {method}: a {method} has no body, and the cache does not key one: use "
                "method='QUERY', the safe read with a body"
            )
            raise TypeError(msg)
        return self._streamed(_Call(method, url, options))

    def events(
        self, url: CallUrl, *, last_event_id: str | None = None, **options: Unpack[StreamOptions]
    ) -> contextlib.AbstractAsyncContextManager[Stream]:
        """Open a server-sent event stream: a :meth:`stream` GET asking for ``text/event-stream``, with no read limit.

        ``async with client.events("feed") as stream:`` then ``async for event in stream.aiter_events():``. It sends
        ``Accept: text/event-stream`` unless the call's own ``headers=`` name an ``Accept``, and gives each read no
        timeout (``math.inf``) unless the call's ``timeout=`` sets ``read``, since an event stream may be quiet for
        minutes. The deadline bounds opening it, as for any stream.

        ``last_event_id=`` sends ``Last-Event-ID`` (when not empty, and in place of one in ``headers=``), to resume the
        stream after the last event a previous :class:`~headgate.streaming.Stream` saw (its ``last_event_id``); wait
        its ``reconnect_after`` first.
        """
        # Merged case-insensitively, so Last-Event-ID replaces one however headers= spelled it.
        headers = httpx2.Headers(options.get("headers"))
        headers.setdefault("Accept", _EVENT_STREAM)
        if bool(last_event_id):
            headers["Last-Event-ID"] = last_event_id
        timeout = options.get("timeout")
        options = options.copy()
        options["headers"] = headers
        if timeout is None:
            options["timeout"] = Timeouts(read=math.inf)
        else:
            check_kind("timeout", timeout, Timeouts, "a Timeouts, such as Timeouts(read=5.0)")
            if timeout.read is None:
                options["timeout"] = replace(timeout, read=math.inf)
        return self.stream(url, **options)

    @overload
    def paginate[I, P](
        self,
        url: CallUrl,
        *,
        pager: Pager[I, P, None],
        max_pages: int | None = None,
        on_limit: Literal["raise", "stop"] = "raise",
        **options: Unpack[CallOptions],
    ) -> Paginated[I, P]: ...
    @overload
    def paginate[I, P, B](
        self,
        url: CallUrl,
        *,
        payload: B | None,
        pager: Pager[I, P, B],
        max_pages: int | None = None,
        on_limit: Literal["raise", "stop"] = "raise",
        **options: Unpack[CallOptions],
    ) -> Paginated[I, P]: ...
    def paginate[I, P, B](
        self,
        url: CallUrl,
        *,
        payload: B | None = None,
        pager: Pager[I, P, B],
        max_pages: int | None = None,
        on_limit: Literal["raise", "stop"] = "raise",
        **options: Unpack[CallOptions],
    ) -> Paginated[I, P]:
        """Return the pages *pager* follows from *url*, each read through the pager's ``read``, iterated lazily.

        ``async for item in client.paginate(...)`` yields the items of each page in turn; ``.pages()`` yields the
        pages. Each page is a GET (a POST, given ``payload=``) through the whole stack, fetched only when the iteration
        reaches it, and a next-page URL taken from a response is sent as an absolute URL, under the same credential
        and destination rules as any other. The pager carries the reader every page is read with, its ``read`` (a
        :class:`~headgate.Reader` or an ``Expect``). Iterating again starts again from the first page. The call options
        (:class:`~headgate.CallOptions`) apply to every page, as they do to one :meth:`get` (or :meth:`post`).

        A walk of POSTs passes its body as ``payload=``: the raw value the pager rewrites page by page (a
        :class:`~headgate.pagination.BodyCursor` or :class:`~headgate.pagination.BodyOffset` needs the value, not its
        bytes), ``None`` for none. The pager's ``write`` (a flavor's ``writer``, see
        :class:`~headgate.pagination.WritingPager`) encodes it for each page, since the writer belongs to the endpoint,
        like the reader and the pager; a pager without one sends the value as JSON, so it must be builtins. A page
        with a body is then a :meth:`post` in every respect: in particular it is re-sent only when that is safe,
        unless ``retry=`` says otherwise.

        *max_pages* is a page budget: at most that many pages are fetched. When more would follow, the walk raises
        :class:`~headgate.errors.PageLimitError`, since the result is incomplete, or with ``on_limit="stop"`` ends
        there. A walk that ends within the budget is unaffected. The walk is a ``Paginated[I, P]``: its items first.

        :raises ValueError: for a ``max_pages`` below 1, an ``on_limit`` that is neither ``"raise"`` nor ``"stop"``,
            ``on_limit="stop"`` without ``max_pages``, a walk its pager refuses
            (:meth:`~headgate.pagination.Pager.first` runs here, once),
            and for a first page with a body and an idempotency key (``idempotency_key=``, or the policy's header in
            ``headers=``, a view's or the client's), as one key on pages with different bodies would have the server
            replay the first page's answer. A later page with a body is held to the same rule, and raises from the
            iteration.
        :raises TypeError: for a keyword no call option names, ``body=``, ``json=`` and ``limit=`` included (a walk's
            body is ``payload=``, its page budget ``max_pages=``), for a ``max_pages`` that is not an ``int``, and for
            a pager whose ``read`` is not a :class:`~headgate.Reader` or an ``Expect``, before the pager runs.
        """
        check_options("paginate", options, CALL_KEYS, _WALK_HINTS)
        check_overrides(options)
        check_page_budget(max_pages, on_limit)
        spec: Expect[P] = _expectation(_page_reader(pager))
        call_options = options.copy()
        # How a body is sent is the walk's: the pager's writer encodes every page's, whatever request the pager returns.
        write = _page_writer(pager)
        first = pager.first(PageRequest(url, call_options.pop("params", None), payload))
        # A page's body cannot carry one fixed key: refused for the first page here, and for a later one (a pager that
        # turns GETs into POSTs) before it is sent.
        fixed_key = self._one_key_for_every_page(call_options)
        if first.payload is not None and fixed_key:
            raise ValueError(_ONE_KEY_FOR_EVERY_PAGE)
        answers = {status: _paired(read) for status, read in spec.answers.items()}
        paired = with_answers(Expect(_paired(spec.ok), headers=spec.headers, accept=spec.accept), answers)

        # The origin the first page was sent to, once it was: a later page's call headers go there and nowhere else.
        addressed: list[httpx2.Origin] = []

        async def fetch(request: PageRequest[B]) -> tuple[P, httpx2.Response]:
            token = _WALK_ORIGIN.set(addressed[0] if addressed and request is not first else None)
            try:
                page, response = await fetch_page(request)
            finally:
                _WALK_ORIGIN.reset(token)
            if not addressed:
                addressed.append((response.history[0] if response.history else response).request.url.origin)
            return page, response

        async def fetch_page(request: PageRequest[B]) -> tuple[P, httpx2.Response]:
            page_options = call_options.copy()
            page_options["params"] = request.params
            if request.payload is None:
                return await self.get(request.url, expect=paired, **page_options)
            if fixed_key:
                raise ValueError(_ONE_KEY_FOR_EVERY_PAGE)
            body = Json(request.payload) if write is None else write(request.payload)
            return await self.post(request.url, expect=paired, body=body, **page_options)

        walk = PageWalk(fetch, pager, first, source=self._source, max_pages=max_pages, on_limit=on_limit)
        return Paginated(walk.pages, pager.page_items)

    def _one_key_for_every_page(self, options: CallOptions) -> bool:
        """Return whether every call made with *options* sends one fixed idempotency key.

        The key is fixed by ``idempotency_key=``, or by the policy's header among those the call sends: its own
        ``headers=``, the view's and the client's ``HttpOptions(headers=...)``, matched in any case.
        """
        keys = self._policy.idempotency
        header = "Idempotency-Key" if keys is None else keys.header
        own = httpx2.Headers(self._view.headers_for(options.get("headers")))
        return "idempotency_key" in options or header in own or header in self._http_client.headers

    # A call, end to end.

    async def _request[T](self, call: _Call, expect: Reader[T] | Expect[T]) -> T:
        """Send *call* and read the reply through its declared responses.

        Every response passes four layers, in order:

        1. **transport** (:meth:`_exchange`): a transport failure, a surviving 429/5xx, or an open breaker raises;
        2. **response check**: the client's ``response_check`` runs on the final response;
        3. **endpoint**: a status the call declared exactly goes to its reader; any other 2xx to the success reader;
        4. **default**: any other non-2xx raises through :func:`~headgate._failures.status_error`.

        See :meth:`_dispatch` for how the last three read the body.
        """
        if call.method in _NO_BODY_HINTS:
            check_options(call.method.lower(), call.options, CALL_KEYS, _NO_BODY_HINTS[call.method])
        else:
            check_options(call.method.lower(), call.options, REQUEST_KEYS, BODY_HINTS)
        check_overrides(call.options)
        spec: Expect[T] = _expectation(expect)
        prepared = self._prepare(call)
        cap = call.options.get("max_response_bytes")
        if cap is not None:
            prepared.context.put(MAX_RESPONSE_BYTES, cap)
        explicit = self._view.headers_for(call.options.get("headers"))
        _stamp_headers(prepared.request, explicit, spec.headers)
        if spec.accept is not None:
            _stamp_headers(prepared.request, explicit, {"Accept": spec.accept})
        async with self._reported(prepared):
            async with self._deadline(prepared), self._exchange(prepared) as (streamed, _):
                announce_deprecation(prepared.context, streamed)
                # The call's cap is for the answers it reads; an undeclared error keeps the client's, so it still
                # raises as its status, not as a body too large for an answer it never asked for.
                answer_cap = cap if streamed.is_success or streamed.status_code in spec.answers else None
                resp = await read_capped(streamed, answer_cap or self._max_response_bytes, self._site(prepared))
            answer = self._dispatch(resp, spec, self._site(prepared))
            # Only now that the reader accepted it may a cached miss be stored.
            await store_accepted(resp)
            return answer

    @contextlib.asynccontextmanager
    async def _streamed(self, call: _Call) -> AsyncGenerator[Stream]:
        """Open *call*'s response as a :class:`~headgate.streaming.Stream` (see :meth:`stream`).

        The exchange is entered under the deadline and left open past it, on the exit stack, so the deadline bounds the
        send and the head checks while the breaker scope and the response last until the caller's block exits.

        The caller's block runs at the ``yield``. What it raises is carried past the exchange in a :class:`_BlockError`,
        out of reach of the exchange's failure mapping, since the stream maps its own network failures as the body
        arrives and anything else is the caller's; it is raised unchanged once the exchange has closed. A
        ``ClientError`` is raised inside the report, as the call's error; any other exception after it, so the call
        reports the exchange it was.
        """
        prepared = self._prepare(call, streamed=True)
        own: Exception | None = None
        async with self._reported(prepared):
            try:
                async with contextlib.AsyncExitStack() as held:
                    streamed, verdict = await self._opened(prepared, held)
                    fail = functools.partial(self._network_failure, prepared, verdict)
                    try:
                        yield OpenStream(streamed, self._site(prepared), fail)
                    except Exception as exc:
                        raise _BlockError(exc) from exc
            except _BlockError as blocked:
                own = blocked.error
            # Raised outside the except clause, so the error keeps its own __context__ rather than the carrier. One
            # another call already reported is that call's, not this stream's.
            if isinstance(own, ClientError) and _REPORTED not in vars(own):
                raise own
        if own is not None:
            raise own

    async def _opened(self, prepared: _Prepared, held: contextlib.AsyncExitStack) -> tuple[httpx2.Response, Verdict]:
        """Send *prepared* under its deadline and check the response head, leaving the exchange open on *held*."""
        async with self._deadline(prepared):
            streamed, verdict = await held.enter_async_context(self._exchange(prepared))
            # The origin answered: settle that now, as a verb would, rather than when the caller's block exits.
            verdict.mark_answered()
            announce_deprecation(prepared.context, streamed)
            if (refused := _checked(self._response_check, streamed)) is not None:
                head = await self._error_head(prepared, streamed, verdict)
                raise refusal_error(refused, head, self._site(prepared)) from refused
            if not streamed.is_success:
                head = await self._error_head(prepared, streamed, verdict)
                raise status_error(head, self._site(prepared))
            # A stream has no reader: passing the head checks is what accepts it.
            await store_accepted(streamed)
        return streamed, verdict

    async def _error_head(self, prepared: _Prepared, streamed: httpx2.Response, verdict: Verdict) -> httpx2.Response:
        """Read a refused stream's error head, mapping a network failure as the exchange would.

        Mapped here, inside the deadline, so a network ``TimeoutError`` is not taken for the deadline's own.
        """
        try:
            return await read_head(streamed, _STREAM_ERROR_HEAD_BYTES)
        except NETWORK_ERRORS as exc:
            raise self._network_failure(prepared, verdict, exc) from exc

    # Preparing the request.

    def _prepare(self, call: _Call, *, streamed: bool = False) -> _Prepared:
        """Build *call*'s request and stamp its context on it; a *streamed* call's context says so (``STREAMED``).

        A stream's own read timeout may be longer than its deadline, which bounds only opening the stream.

        Raises ``ValueError`` for a t-string value no path segment can hold, for a call that would follow redirects
        on a client with a ``response_check``, for a ``deadline=`` that is not positive and finite and for a ``Json``
        body JSON cannot carry; ``TypeError`` for a URL that is neither a ``str``, a t-string nor an absolute
        ``httpx2.URL``, and for a ``body=`` that is no ``Body``; and ``ClientError`` for a URL httpx2 cannot parse.
        """
        _check_url(call.url)
        if isinstance(call.url, Template):
            url, route = render_path(call.url), route_template(call.url)
        else:
            url, route = str(call.url), None
        log_url = loggable_url(url)
        options = call.options
        follow = self._follows(options)
        # The per-call half of the constructor's check: the client itself never follows with a response_check.
        if self._response_check is not None and follow:
            msg = f"the client of {self._source} has a response_check=, so no call may follow redirects"
            raise ValueError(msg)
        params = options.get("params")
        body = lowered(options.get("body"))
        try:
            request = self._http_client.build_request(
                call.method,
                url,
                params=None if params is None else _query_pairs(params),
                headers=self._view.headers_for(options.get("headers")),
                data=body.data,
                content=body.content,
                files=body.files,
                timeout=httpx_timeout(self._timeout, options.get("timeout")),
            )
        except httpx2.InvalidURL as exc:
            raise _invalid_url(exc, CallSite(self._source, call.method, log_url)) from exc
        content_type = body.content_type if body.content_type is not None else _encoded_type(request, body)
        if content_type is not None:
            _stamp_headers(request, self._view.headers_for(options.get("headers")), {"Content-Type": content_type})
        own = options.get("headers") or {}
        if self._view.headers or own:
            request.extensions[EXPLICIT_HEADERS] = ExplicitHeaders(
                origin=_WALK_ORIGIN.get() or request.url.origin,
                view=frozenset(name.lower() for name, _ in self._view.headers),
                call=frozenset(name.lower() for name in own),
            )
        # The absolute URL names the server to an observer (an OpenTelemetry span's url.full) and to a middleware.
        info = CallInfo(method=call.method, url=log_url, template=route, full_url=sanitize_url(str(request.url)))
        # The call's route is matched once, here; every layer reads what it set from the call's context.
        path = url_path(request.url)
        route_policy = match_route(self._policy.routes, path)
        route_options = {} if route_policy is None else route_policy.options
        self._stamp_route_timeout(request, route_options, options.get("timeout"))
        deadline = self._deadline_of(call, route_options)
        if (timeout := options.get("timeout")) is not None:
            # Its kind was checked building the request (httpx_timeout), so a stream's is a Timeouts here.
            check_timeout_fits(replace(timeout, read=None) if streamed else timeout, deadline)
        context = CallContext(info, clock=self._timing.clock, deadline_after=deadline, emitter=self._emitter)
        if streamed:
            context.put(STREAMED, value=True)
        context.follow_redirects = follow
        self._stamp_route(context, call, route_options, path)
        _mark_body(context, body, given=options.get("body") is not None)
        self._stamp_idempotency_key(request, context, options.get("idempotency_key"))
        # The cache transport only setdefaults the client's own TTL, so a per-call TTL stamped here wins.
        if "cache_ttl" in options:
            stamp_ttl(request, options["cache_ttl"])
        context.attach(request)
        return _Prepared(request, context, follow)

    def _stamp_route_timeout(self, request: httpx2.Request, route: RouteOptions, call: Timeouts | None) -> None:
        """Send *request* with its *route*'s timeout, under the view's and the *call*'s own, over the policy's."""
        if "timeout" not in route:
            return
        under = route["timeout"].over(self._policy.timeout)
        if self._view_timeout is not None:
            under = self._view_timeout.over(under)
        request.extensions["timeout"] = httpx_timeout(under, call).as_dict()

    def _stamp_route(self, context: CallContext, call: _Call, route: RouteOptions, path: str) -> None:
        """Put on *context* what *call*'s *route* sets for every layer below: its retry policy, budget and cache."""
        if (retry := self._retry_of(call, route)) is not None:
            context.put(RETRY_POLICY, retry)
        # Every hop of the call, a redirect's included, spends from the budget its route names.
        context.put(CALL_BUDGET, self._budgets.for_path(path))
        if "cache" in route:
            context.put(UNCACHED, value=True)

    def _deadline_of(self, call: _Call, route: RouteOptions) -> float | None:
        """Return the seconds *call* may take: its own ``deadline=``, else the view's, its *route*'s, the policy's."""
        if "deadline" in call.options:
            deadline = call.options["deadline"]
            check_deadline(deadline)
            return deadline
        if self._view.deadline is not KEEP:
            return self._view.deadline
        if "deadline" in route:
            return route["deadline"]
        return self._policy.deadline

    def _retry_of(self, call: _Call, route: RouteOptions) -> RetryPolicy | None:
        """Return *call*'s own retry policy when it overrides the client's: per call, view or *route*; else ``None``.

        A call's, a view's or a route's ``retry=None`` sends once; the policy's ``None`` is read where the stack is
        built.
        """
        if self._policy_off:
            return SEND_ONCE
        if "retry" in call.options:
            retry = call.options["retry"]
            return SEND_ONCE if retry is None else retry
        if self._view.retry is not KEEP:
            return self._view.retry
        if "retry" in route:
            retry = route["retry"]
            return SEND_ONCE if retry is None else retry
        return None

    def _follows(self, options: CallOptions | RequestOptions | StreamOptions) -> bool:
        """Return whether a call with *options* follows redirects: its own choice, else the view's, the client's."""
        if "follow_redirects" in options:
            return options["follow_redirects"]
        if self._view.follow_redirects is not KEEP:
            return self._view.follow_redirects
        return self._http_client.follow_redirects

    def _stamp_idempotency_key(self, request: httpx2.Request, context: CallContext, key: str | None) -> None:
        """Put the call's idempotency key on *request* once, before the retry layer, so every attempt carries it.

        The key is the call's ``idempotency_key=``, else a fresh one from the policy's ``IdempotencyKeys`` for a POST
        or PATCH. A key the caller already set in ``headers=`` is kept. Raises ``ValueError`` for an empty or blank key.
        """
        keys = self._policy.idempotency
        header = "Idempotency-Key" if keys is None else keys.header
        if key is not None and not key.strip():
            msg = f"idempotency_key= takes a key that is not empty or blank (got {key!r})"
            raise ValueError(msg)
        if key is None and keys is not None and request.method in KEYED_METHODS:
            key = keys.factory()
            if not key.strip():
                msg = f"IdempotencyKeys(factory=...) must return a key that is not empty or blank (got {key!r})"
                raise ValueError(msg)
        if key is not None:
            request.headers.setdefault(header, key)
        context.idempotency_key = request.headers.get(header)

    # Around the exchange: events and the deadline.

    @contextlib.asynccontextmanager
    async def _reported(self, prepared: _Prepared) -> AsyncGenerator[None]:
        """Emit ``CallStarted`` and, on the way out, ``CallFinished``; add the call's notes to a ``ClientError``.

        A ``ClientError`` leaving the call takes the call's :attr:`~headgate.ClientError.sent`, whichever layer
        raised it, since what a caller needs to know is whether the call's own request can have left.
        """
        context = prepared.context
        if context.observed:
            context.emit(CallStarted(call=context.info))
        error: str | None = None
        try:
            yield
        except BaseException as exc:
            error = type(exc).__name__
            # Only the first call to report an error notes it: an inner call's error passing through a stream's block.
            if isinstance(exc, ClientError) and _REPORTED not in vars(exc):
                vars(exc)[_REPORTED] = True
                for note in context.notes:
                    exc.add_note(note)
                # What the call's own request sent, whichever layer raised the error: a token mint, a middleware.
                vars(exc)["_sent"] = context.sent
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
            async with asyncio.timeout(remaining) as timer:
                prepared.context.put(_DEADLINE_TIMER, timer)
                yield
        except TimeoutError:
            context = prepared.context
            if context.progress.dispatched or context.attempt == 0:
                site, unsent = self._site(prepared), ""
            else:
                site, unsent = self._unsent_site(prepared), f", before attempt {context.attempt} was sent"
            msg = f"{site.method} {site.url}: deadline exceeded after {_seconds(context.elapsed())}{unsent}"
            raise ClientTimeoutError(msg, **site.details()) from None

    # The exchange: one send under the origin's breaker.

    @contextlib.asynccontextmanager
    async def _exchange(self, prepared: _Prepared) -> AsyncGenerator[tuple[httpx2.Response, Verdict]]:
        """Send the request under its origin's circuit breaker; yield the streamed response, and the breaker's verdict.

        The response is closed afterwards. The verdict lets a reader of the body outside this scope charge a network
        failure to the origin (:meth:`_network_failure`).

        The response yielded is anything but a 429 or 5xx: one of those means the retry transport ran out of
        attempts, and it is raised here, so no call can declare either as an answer. A network failure or timeout,
        in the send or in a verb's body read inside the block, becomes ``TransportError`` /
        ``ClientTimeoutError``; an open breaker ``CircuitOpenError``; a rate budget the server paused past the
        call's deadline ``RateLimitError``; and a hop the SSRF guard refused, or a token mint's hop redirected off its
        origin, ``ForbiddenDestinationError``.

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
                context.put(BREAKER_GATE, verdict.refused)
                async with self._sent(prepared, verdict) as streamed:
                    yield streamed, verdict
        except BreakerOpenError as exc:
            # Refused unsent, so sending again is safe whatever the method, and admitted once the cooldown has passed.
            details = self._site(prepared).details(retry_after=exc.retry_after)
            details["retryable"] = True
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
            # Not the retry policy's call: the budget says when sending again will be admitted. The call got no
            # response, so it carries no status, rather than the one the response that caused the pause had.
            details = self._site(prepared).details(retry_after=exc.retry_after)
            details["retryable"] = True
            raise RateLimitError(str(exc), status_code=None, **details) from None
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
          failing to serve the request as surely as a 503), and a network failure or timeout. The call's deadline
          cutting off an attempt already handed to the network, before its head or mid-body, is such a timeout: the
          deadline, not httpx2's own timeout, is what ends a call to an origin that hangs;
        * **nothing**: a 429 (the origin is up and asked us to slow down, which the rate budget handles); a network
          error of our own side's making, or a redirect loop (:data:`_LOCAL_FAILURES`, and an invalid URL); a failure
          on a hop an auth flow made to mint a credential (that is the credential host's failure); and an outcome
          another request's attempt produced (a cache hit, a stored 5xx served from the cache, or a download or failure
          the cache shared from an identical GET, :data:`~headgate.cache._transport.HOST_UNTESTED`), since only the
          request that fetched tested the origin. The caller's task being cancelled, and the deadline passing before
          the attempt reached the network (in a rate-budget wait, a middleware, a token mint) or while the auth mints,
          are nothing too, as is a response a middleware answered without sending the request.
        """
        request = prepared.request
        try:
            streamed = await self._http_client.send(request, stream=True, follow_redirects=prepared.follow_redirects)
            prepared.report.status_code = streamed.status_code
            try:
                _charge_response(streamed, verdict, prepared.context)
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
        except asyncio.CancelledError:
            if _cut_off_on_the_wire(prepared.context):
                verdict.mark_failed()
            raise

    def _network_failure(self, prepared: _Prepared, verdict: Verdict, exc: httpx2.HTTPError | OSError) -> ClientError:
        """Charge the network failure *exc* to the origin on *verdict*, and return the ``ClientError`` it becomes."""
        if (mint_failure := mint_hop_failure(exc)) is not None:
            return mint_failure
        if _network_failure_charges_origin(exc):
            verdict.mark_failed()
        return transport_error(exc, self._site(prepared))

    def _breaker_for(self, url: httpx2.URL) -> Breaker:
        """Return *url*'s origin's breaker from the client's registry, or a no-op one when the client has no breaker."""
        return NO_BREAKER if self._breakers is None else self._breakers.breaker_for(url)

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
        """Read the final *resp* through the response check, the call's declared responses and the status ladder.

        A ``RefusedError`` from a reader, or raised or returned by the ``response_check``, becomes the ``ClientError``
        its kind names, with the real status. A reader that finds the body is not the shape it expected (a codec's
        ``DecodeError``, or the ``ValueError``, ``LookupError`` or ``TypeError`` hand-written code over
        ``response.json()`` raises, or nesting too deep to parse) on a 2xx raises ``ResponseParseError`` naming the
        status and request line; on a declared non-2xx it falls through to the status ladder, because a declaration
        covers only the body shape it names. A body whose ``Content-Type`` is not the one ``Expect(accept=...)`` asked
        for is such a shape error.
        """
        if (refused := _checked(self._response_check, resp)) is not None:
            raise refusal_error(refused, resp, site) from refused
        read = spec.answers.get(resp.status_code)
        if read is None:
            if not resp.is_success:
                raise status_error(resp, site)
            read = spec.ok
        try:
            if not accepts(spec, resp):
                content_type = resp.headers.get("content-type") or "no Content-Type"
                msg = f"expected {spec.accept}, got {content_type}"
                raise DecodeError(msg)
            return read(resp)
        # The one place a reader's RefusedError is caught: it becomes the ClientError carrying the status the wire sent.
        except RefusedError as refused:
            raise refusal_error(refused, resp, site) from refused
        # Neither error is chained to the reader's: a validation library's error may quote the input it refused (a
        # token, say), which a traceback or logger.exception would repeat.
        except SHAPE_ERRORS as exc:
            if resp.is_success:
                raise parse_error(exc, resp, site) from None
            raise status_error(resp, site) from None

    def _site(self, prepared: _Prepared) -> CallSite:
        """Return where *prepared*'s call is now, for an error raised about it: the attempt count is read now."""
        context = prepared.context
        info = context.info
        return CallSite(
            self._source,
            info.method,
            info.url,
            context.attempt,
            self._request_id_headers,
            context.get(RETRY_OUTCOME),
            self._timing.wall_clock,
        )

    def _unsent_site(self, prepared: _Prepared) -> CallSite:
        """Return :meth:`_site` for a call whose current attempt never reached the network: it does not count."""
        return replace(self._site(prepared), attempts=prepared.context.attempt - 1)


def _mark_body(context: CallContext, body: Lowered, *, given: bool) -> None:
    """Say on *context* what the layers below need of a call's *body*: that it was *given*, and if it is one-shot."""
    if given:
        context.put(HAS_BODY, value=True)
    if not body.replayable:
        context.put(REPLAYABLE_BODY, value=False)


def _check_url(url: object) -> None:
    """Refuse a call's *url* unless it is a ``str``, a t-string or an absolute ``httpx2.URL``.

    A relative ``httpx2.URL`` is the one escape from escaping a t-string does not make greppable, and every URL a pager
    forwards from the wire is absolute, so it has no other use.

    :raises TypeError: naming what to write instead.
    """
    check_kind("url", url, (str, Template, httpx2.URL), "a literal str, a t-string or an absolute httpx2.URL")
    if isinstance(url, httpx2.URL) and not url.is_absolute_url:
        msg = (
            "a relative httpx2.URL skips escaping: write a t-string, t'items/{uid}', or {fragment:raw} for a "
            f"pre-built fragment (got {loggable_url(str(url))!r})"
        )
        raise TypeError(msg)


def _check_auth(auth: object) -> None:
    """Refuse an *auth* that is not a :class:`~headgate.Auth`, whose credentials the guard could not know to withhold.

    A subclass is required, not ``isinstance``: ``Auth.register`` makes a virtual subclass that declares nothing. So
    is a class that went through ``Auth.__init_subclass__``, which a base that skips ``super()`` cuts off.

    :raises TypeError: naming its class and what to write instead.
    """
    if auth is not None and Auth not in type(auth).__mro__:
        msg = (
            f"auth= takes a headgate.Auth, which declares what it stamps (got {type(auth).__qualname__}): subclass "
            "headgate.Auth, or use StaticHeaderAuth, and declare the headers and query parameters it sets in "
            "credential_headers and credential_params"
        )
        raise TypeError(msg)
    if isinstance(auth, Auth) and not was_checked(auth):
        msg = (
            f"auth= takes a headgate.Auth whose class was checked when it was defined, and {type(auth).__qualname__} "
            "was not checked when it was defined: a base class's __init_subclass__ skips super().__init_subclass__(), "
            "so its subclasses escape the checks of what they stamp and how their hooks are written. Call "
            "super().__init_subclass__(**kwargs) there"
        )
        raise TypeError(msg)


def _layered_timeout(over: Timeouts | None, under: Timeouts | None) -> Timeouts | None:
    """Return the phases *over* sets laid over *under*'s, either of which may be ``None`` for none."""
    if over is None or under is None:
        return under if over is None else over
    return over.over(under)


def _route_rate(route: Route) -> tuple[Rate | RateBudget | None, bool]:
    """Return the rate *route* sets, and whether it sets one; ``None`` paces nothing of the route's own."""
    options = route.options
    return (options["rate"], True) if "rate" in options else (None, False)


def _stack_parts[T](name: str, parts: Sequence[T]) -> tuple[T, ...]:
    """Return a client's ``observers`` or ``middleware``, *parts*, as a tuple.

    :raises TypeError: for a lone object or a ``str`` where a sequence of them goes, and for an upstream-httpx object.
    """
    try:
        items = None if isinstance(parts, str) else tuple(parts)
    except TypeError:  # A lone observer or middleware, which an untyped caller may pass.
        items = None
    if items is None:
        msg = f"{name}= takes a sequence, such as a tuple, of them (got {type(parts).__name__})"
        raise TypeError(msg)
    check_hooks(name, items)
    return items


def _effective_policy(policy: CallPolicy, *, off: bool) -> CallPolicy:
    """Return the policy a client built now runs: *policy*, less retries and the breaker when call policy is *off*.

    The rate budget checks for call policy being off itself, when it is built.
    """
    return replace(policy, retry=None, breaker=None) if off else policy


def _registry_of(policy: CallPolicy, timing: Timing) -> BreakerRegistry | None:
    """Return the registry a client's breakers live in: the shared one it is given, else one of its own on *timing*."""
    breaker = policy.breaker
    if breaker is None or isinstance(breaker, BreakerRegistry):
        return breaker
    # A registry reads the current timing when it is built: this one is built on the client's.
    with timing_override(timing):
        return BreakerRegistry(breaker)


def _query_pairs(params: Mapping[str, object]) -> list[tuple[str, WireValue]]:
    """Flatten *params* into the ``(key, value)`` pairs httpx2 encodes: a list or tuple repeats its key.

    :raises TypeError: for a value with no wire meaning (see :func:`~headgate._request_body.wire_value`), a list or
        tuple inside another among them, before anything is sent.
    """
    pairs: list[tuple[str, WireValue]] = []
    for key, value in params.items():
        items = value if isinstance(value, list | tuple) else (value,)
        pairs.extend((key, wire_value("params=", key, item)) for item in items)
    return pairs


def _encoded_type(request: httpx2.Request, body: Lowered) -> str | None:
    """Return the ``Content-Type`` httpx2 encoded a ``Form`` or ``Multipart`` body with; ``None`` for others.

    httpx2 sets it only under the client's default, so a client-wide ``Content-Type`` would replace it (and a multipart
    body would lose its boundary).
    """
    if not (bool(body.data) or bool(body.files)):
        return None
    # httpx2's multipart stream names its boundary; any other data= body is form-encoded.
    multipart = getattr(request.stream, "content_type", None)
    return multipart if isinstance(multipart, str) else "application/x-www-form-urlencoded"


def _stamp_headers(request: httpx2.Request, explicit: Mapping[str, str] | None, headers: Mapping[str, str]) -> None:
    """Send *headers* on *request*, each over the client's default but never over an *explicit* one.

    *explicit* is what the call and its view set in ``headers=``. The headers stamped this way are the ones the call's
    own declarations imply: its body's ``Content-Type``, and its ``Expect``'s ``headers`` and ``accept``.
    """
    taken = set() if explicit is None else {name.lower() for name in explicit}
    for name, value in headers.items():
        if name.lower() not in taken:
            request.headers[name] = value


def _charge_response(response: httpx2.Response, verdict: Verdict, context: CallContext) -> None:
    """Mark *verdict* with what *response* says about its origin (see :meth:`Client._sent`).

    A response another request's attempt produced (a cache hit, a stored response, a shared download) or that never
    reached the network (a middleware's own answer) is untested; a 5xx this request fetched itself is a failure; a 429
    is neither, since the origin is up and asked us to slow down.
    """
    # shortcut: hedges share one progress record, so a hedge still queued in the rate budget when the first send answers
    # makes that answer read as unsent (charged nothing); mark sent responses in MiddlewareTransport if that matters.
    if not context.progress.dispatched or host_untested(response) or served_from_cache(response):
        verdict.mark_untested()
    elif response.status_code >= httpx2.codes.INTERNAL_SERVER_ERROR:
        verdict.mark_failed()


def _network_failure_charges_origin(exc: Exception) -> bool:
    """Return whether a network failure is the origin's, to charge its breaker with (see :meth:`Client._sent`).

    Not when it is of our own side's making or a redirect loop (:data:`_LOCAL_FAILURES`), happened on a hop an auth
    flow made to mint a credential, or was shared by the cache from another request's attempt.
    """
    return not (isinstance(exc, _LOCAL_FAILURES) or failed_on_auth_hop(exc) or host_untested(exc))


def _seconds(seconds: float) -> str:
    """Return *seconds* as an error message writes them: to the hundredth, without trailing zeros, with an ``s``.

    Never in scientific notation, which ``:g`` falls into from 1000 s on.
    """
    return f"{f'{seconds:.2f}'.rstrip('0').rstrip('.')}s"


def _cut_off_on_the_wire(context: CallContext) -> bool:
    """Return whether the call's own deadline fired while its attempt was on the network (see ``_sent``).

    The deadline's timer says whether the cancellation is its own rather than the caller's; the attempt was on the
    network once the rate budget handed it on, unless the auth flow was minting (that is the credential host's time).
    """
    timer = context.get(_DEADLINE_TIMER)
    return timer is not None and timer.expired() and context.progress.dispatched and context.get(MINTING) is not True


def _checked(check: ResponseCheck | None, resp: httpx2.Response) -> RefusedError | None:
    """Run the client's *check* on *resp*, treating a ``RefusedError`` it raises like one it returns."""
    if check is None:
        return None
    try:
        return check(resp)
    except RefusedError as refused:
        return refused


def _expectation[T](expect: Reader[T] | Expect[T]) -> Expect[T]:
    """Return *expect* as an ``Expect``: itself, or one whose success reader it is.

    :raises TypeError: for an *expect* that is neither, such as a model class or a bare function.
    """
    check_kind("expect", expect, (Reader, Expect), "a Reader or an Expect")
    return expect if isinstance(expect, Expect) else Expect(expect)


def _page_reader[I, P, B](pager: Pager[I, P, B]) -> Reader[P] | Expect[P]:
    """Return the reader *pager* reads its pages with, its ``read``.

    :raises TypeError: for a pager with no ``read``, or a ``read`` that is neither a :class:`~headgate.Reader` nor an
        ``Expect``, naming what it got.
    """
    read: object = getattr(pager, "read", None)
    if not isinstance(read, Reader | Expect):
        got = "no read" if read is None else f"a read of {named(read)}"
        msg = (
            f"paginate()'s pager reads each page with its read, a Reader or an Expect (the {type(pager).__name__} has "
            f"{got}): pass the page reader first, as in Cursor(reader(Page), items=..., next=..., param=...)"
        )
        raise TypeError(msg)
    return cast("Reader[P] | Expect[P]", read)


def _page_writer[I, P, B](pager: Pager[I, P, B]) -> Writer[B] | None:
    """Return the writer *pager* encodes each page's body with: a writing pager's ``write``, else ``None``.

    :raises TypeError: for a :class:`~headgate.pagination.WritingPager` with no ``write``.
    """
    if not isinstance(pager, WritingPager):
        return None
    if not hasattr(pager, "write"):
        msg = (
            f"paginate()'s pager is a WritingPager, so it encodes each page's body with its write (the "
            f"{type(pager).__name__} has none): give it write, a flavor's writer, or None for a body of builtins sent "
            "as JSON"
        )
        raise TypeError(msg)
    return pager.write


def _paired[T](read: Reader[T]) -> Reader[tuple[T, httpx2.Response]]:
    """Return a reader that returns what *read* reads together with the response, for a pager to read headers from."""

    def _read(resp: httpx2.Response, /) -> tuple[T, httpx2.Response]:
        return read(resp), resp

    return Reader(_read, name=f"paired {read!r}")


def _invalid_url(exc: httpx2.InvalidURL, site: CallSite) -> ClientError:
    """Build the ``ClientError`` for a URL httpx2 rejected, naming it and httpx2's reason, both bounded."""
    return ClientError(f"invalid URL {site.url}: {shorten(str(exc))}", **site.details())
