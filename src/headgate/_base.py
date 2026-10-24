"""Async-first base client for all data clients.

Provides :class:`AsyncBaseClient`, a thin policy layer over ``httpx2.AsyncClient`` that adds retries, rate limiting,
per-host circuit breaking, a response-size cap, and the ``ClientError`` mapping — concerns httpx2 itself doesn't handle.

A call runs three steps, each in one place:

1. :meth:`AsyncBaseClient._request` renders the URL, builds the ``httpx2.Request`` once, and stamps its three
   extensions (the retry override, a per-call cache TTL, the redirect marker);
2. :meth:`AsyncBaseClient._send` runs the round trip under the host's circuit breaker: send, cap the body
   (:func:`headgate._body.read_capped`), and map a transport failure or a surviving 429/5xx onto the
   ``ClientError`` tree (:mod:`headgate._failures`);
3. back in ``_request``, the response is read through the call's declared responses, in four layers: transport (step
   2), host (the client's own ``refusal=`` check), endpoint (the declared reader for an exact status, else the success
   reader for any other 2xx), and default (the status ladder for anything undeclared).

There is **no ``None`` return** except from a reader that returns it (:data:`~headgate._responses.nothing`):
every undeclared non-2xx raises a ``ClientError`` subtype, so no call can receive one unread. A non-2xx the provider
DOCUMENTS as an answer is declared at the call and comes back decoded as its own wire type (a documented 404 that
carries a typed body); what it MEANS is the caller's. A refusal written
inside a 2xx body raises with that 2xx status and the provider's code. Otherwise no-data is a CONTENT judgement — a 200
carrying an empty envelope.

The transport stack — retry, the opt-in cache, and rate limiting as the bottom layer, so a cache hit spends no token
and each retry spends its own — is assembled by :func:`headgate._stack.build_http_client`. The redirect hooks live
in :mod:`headgate._redirects`. URL joining, headers, timeouts and connection pooling are httpx2's.

Subclasses configure themselves via ``__init__`` parameters, passed through
``super().__init__(base_url=..., rate=..., ...)``::

    class FredClient(AsyncBaseClient):
        def __init__(self, api_key: str) -> None:
            super().__init__(
                base_url="https://api.stlouisfed.org/fred",
                rate=Rate.per_minute(120),
            )
            self._api_key = api_key

        async def fetch_series(self, series_id: str):
            from headgate.msgspec import decoder

            return await self.get("series/observations", expect=decoder(FredObs), params={...})
"""

from __future__ import annotations

import contextlib
from string.templatelib import Template
from typing import TYPE_CHECKING, Literal, Self, TypedDict, Unpack

import httpx2

from headgate._async_breaker import NO_BREAKERS, CircuitOpenError, _host_breakers
from headgate._auth import failed_on_auth_hop
from headgate._body import DEFAULT_MAX_RESPONSE_BYTES, read_capped, read_head
from headgate._failures import refusal_error, status_error, transient_status_error, transport_error
from headgate._redirects import FOLLOW_REDIRECTS_EXT
from headgate._responses import Expect, Reader, Refusal, Refused
from headgate._stack import build_http_client
from headgate._transports import build_retry
from headgate._url import path as render_path
from headgate._url import sanitize_url
from headgate.errors import ClientError, DecodeError, ResponseParseError, TransportError
from headgate.policy import policy_is_disabled

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping, Sequence
    from types import TracebackType

    from headgate._async_breaker import Breakers
    from headgate._cache import HttpCache
    from headgate._rate_limiter import Rate
    from headgate._transports import RateRule

__all__ = ["AsyncBaseClient", "RequestOptions"]

#: How much of a refused or failed streamed response is read, for the ``ClientError``'s body snippet.
_STREAM_ERROR_HEAD_BYTES = 4096

# The HTTP verbs the public methods delegate to _request with.
_Method = Literal["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"]


class RequestOptions(TypedDict, total=False):
    """The request arguments a verb forwards to ``httpx2.AsyncClient.build_request``, typed.

    ``params`` is ``Mapping[str, object]`` because clients pass a TypedDict for their query parameters, and a TypedDict
    is assignable to no narrower mapping; httpx2 stringifies each value. The options that are not request arguments — ``retry``,
    ``follow_redirects``, ``cache_ttl`` — are explicit keywords on the verbs instead, since ``build_request`` has no
    parameter for them. A raw body (``content=``) and files are not here because no client sends one.
    """

    params: Mapping[str, object] | None
    headers: Mapping[str, str]
    json: object
    data: Mapping[str, object]
    timeout: float


#: The policy a ``retry=False`` call installs: zero retries, so the request is sent once and never re-sent.
#:
#: Built once at import rather than per call. A :class:`~headgate._transports.Retry` is immutable
#: (``increment()`` returns a copy), so one shared instance is safe. ``RetryTransport`` reads it with
#: ``request.extensions.setdefault("retry", …)``, which is what makes it win over the client's default. (The
#: identity comparison an earlier comment here described, ``override is not self.retry``, belonged to a
#: ``_RetryTransport`` subclass that went when httpx-retries 0.6.0 made the override native.)
_ZERO_RETRY = build_retry(0)


# AsyncBaseClient


class AsyncBaseClient:
    """Async-first base for HTTP data clients.

    Configuration flows through ``__init__`` and is consumed at construction. The instance state is
    ``self._http_client`` (the per-subclass ``httpx2.AsyncClient`` from :func:`~headgate._stack.build_http_client`;
    the limiters live in its transport stack, where a ``rate_limits`` rule gives a matching URL path its own),
    ``self._breakers`` (the shared per-host registry, or ``NO_BREAKERS``), and the two
    values a request needs: ``self._refusal`` and ``self._max_response_bytes``.

    Implements the async context manager protocol directly: ``__aenter__`` returns ``self``; ``__aexit__`` delegates to
    :meth:`aclose`.

    **Failure policy —** every verb takes the call's declared responses as ``expect=`` (see
    :mod:`headgate._responses`) and returns what the matching reader read, or raises a
    ``headgate.errors.ClientError`` subtype: ``TransportError`` / ``ClientTimeoutError`` (network/timeout, an
    open breaker) / ``RateLimitError`` (a surviving 429, or a quota refusal a reader's ``check`` found inside a 200)
    / ``UpstreamStatusError`` (a surviving 5xx, a status the call did not declare, or an in-band refusal carrying
    the 2xx it arrived with; 401/403 → ``AuthError``) / ``ResponseParseError`` (an undecodable 2xx body). A boundary
    can map these to HTTP 502/504. There is **no ``None`` return** except from a reader that returns it
    (:data:`~headgate._responses.nothing`): every undeclared non-2xx always raises, a 404 included, so no call can
    receive one unread. A non-2xx the provider documents as an answer is declared at the call and comes back decoded as
    its own wire type. Otherwise no-data is what a 200 carrying an empty envelope means. A best-effort caller that wants
    to degrade catches ``ClientError`` at its own walk-loop and decides there.

    .. rubric:: Why every client shares one connection pool

    ``max_connections=10`` / ``max_keepalive_connections=5`` are module constants in :mod:`headgate._stack`,
    identical for every client class, and that is correct rather than an oversight. The RATE LIMITER is the intended
    throttle and is tuned per provider; the pool only has to be wide enough never to become the binding constraint
    first. Required concurrency is ``rate x latency``, so even at a generous 300 ms per request a 10-connection pool
    sustains any budget up to ~33 req/s. A client budgeted well above that should pass its own ``transport=`` with a
    wider pool, sized on a MEASURED latency and not on this arithmetic, which assumes a latency nobody has recorded.
    """

    def __init__(
        self,
        *,
        base_url: str,
        rate: Rate,
        max_retries: int = 3,
        default_headers: dict[str, str] | None = None,
        timeout: float = 30.0,
        rate_limits: Sequence[RateRule] = (),
        transport: httpx2.AsyncBaseTransport | None = None,
        circuit_breaker: bool = True,
        cache: HttpCache | None = None,
        auth: httpx2.Auth | None = None,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
        follow_redirects: bool = True,
        refusal: Refusal | None = None,
    ) -> None:
        # Transport policy off (headgate.policy.policy_disabled): retries, the breaker and the throttle are all
        # production behaviours that are actively wrong against a fake network or in a probe that must report the
        # FIRST failure. Read here because this is where the stack is assembled and frozen.
        if policy_is_disabled():
            max_retries, circuit_breaker = 0, False
        # A refusal check sees only the FINAL response, so a client following redirects would skip one written on a
        # hop. The per-call half is in _request.
        if refusal is not None and follow_redirects:
            raise ValueError(
                f"{type(self).__name__}: a client with a refusal= check must be built with follow_redirects=False"
            )
        self._refusal = refusal
        # The circuit breaker is not a transport layer: _send drives it around the send and the body read (see
        # headgate._async_breaker), which a transport cannot span. ``circuit_breaker=False`` turns it off for a client
        # where "this host is failing, stop calling it" is meaningless, such as an in-process ASGI self-call that has no
        # host to be down. NO_BREAKERS never reads the shared registry, so a breaker another client opened cannot refuse
        # this one.
        self._breakers: Breakers = _host_breakers if circuit_breaker else NO_BREAKERS
        self._max_response_bytes = max_response_bytes
        self._http_client = build_http_client(
            base_url=base_url,
            rate=rate,
            rate_limits=rate_limits,
            transport=transport,
            cache=cache,
            max_response_bytes=max_response_bytes,
            max_retries=max_retries,
            auth=auth,
            default_headers=default_headers,
            follow_redirects=follow_redirects,
            timeout=timeout,
        )

    # Lifecycle — ``__aenter__`` returns ``self``; ``aclose`` is the explicit
    # cleanup entry point; ``__aexit__`` delegates to it.

    async def __aenter__(self) -> Self:
        return self

    async def aclose(self) -> None:
        await self._http_client.aclose()

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc_value: BaseException | None, traceback: TracebackType | None
    ) -> None:
        await self.aclose()

    # HTTP request + decode

    async def _send(self, request: httpx2.Request, *, log_url: str, follow_redirects: bool | None) -> httpx2.Response:
        """One round trip under the host's circuit breaker: send, cap the body, map a failure onto the ClientError tree.

        Returns any response that is not a 429 or 5xx — 2xx, 3xx and non-transient 4xx — for :meth:`_request` to read.
        Raises ``TransportError`` / ``ClientTimeoutError`` for a network or timeout failure, the matching
        ``ClientError`` for a 429/5xx the retry transport could not get past, and ``TransportError`` for an open
        breaker. Retry and pacing happen BELOW this method, inside the transport stack, so an open breaker costs no
        token and a cache hit is never paced.

        The breaker is driven HERE rather than by a transport layer, because this is the only scope that spans the
        send AND the body read — a connection dying mid-body is a failure of the fetch, and a transport wrapped around
        the send sees only the headers. It is keyed on the host the request is addressed to; the hops of a followed
        redirect are charged to that host. The verdict is marked at the only points that know what the wire said: a
        transport error or timeout and a surviving 429/5xx are host failures, and a cache hit tested nothing.
        Anything else that escapes — an auth flow failing to mint, a body over the cap, a CancelledError from the
        caller's own ``wait_for`` — records nothing (see ``AsyncCircuitBreaker.track``).
        """
        # Nothing here logs (see the package docstring), so the request line (``{method} {url}``, sanitized) is folded
        # into the error messages. HTTP timing/spans come from whatever tracing instrumentation the application
        # installs on httpx2.
        source = type(self).__name__
        try:
            with self._breakers[request.url.host].track() as verdict:
                try:
                    # Streamed rather than buffered so the body can be capped as it arrives (see read_capped).
                    streamed = await self._http_client.send(
                        request,
                        stream=True,
                        follow_redirects=httpx2.USE_CLIENT_DEFAULT if follow_redirects is None else follow_redirects,
                    )
                    resp = await read_capped(streamed, self._max_response_bytes, source=source, url=log_url)
                except (httpx2.HTTPError, OSError) as exc:
                    # A hop the auth flow yielded to mint a credential failed: that is the credential host's, not
                    # this one's, so it must not count towards opening this host's breaker.
                    if not failed_on_auth_hop(exc):
                        verdict.host_failed()
                    raise transport_error(exc, request.method, log_url, source=source) from exc
                # A cache HIT reached no network, so it tells the breaker nothing about the host.
                if resp.extensions.get("hishel_from_cache"):
                    verdict.not_a_host_test()
                # A returned 429/5xx means the retry transport exhausted its attempts. It raises here, because no call
                # may declare either as an answer (``_responses._refuse_unclaimable``).
                if resp.status_code == 429 or resp.status_code >= 500:
                    verdict.host_failed()
                    raise transient_status_error(resp, source=source)
                return resp
        except CircuitOpenError as exc:
            raise TransportError(str(exc), source=source) from None

    async def stream_get(
        self,
        url: str | Template,
        *,
        follow_redirects: bool | None = None,
    ) -> AsyncIterator[bytes]:
        """HTTP GET whose 2xx body is yielded chunk by chunk, never buffered and never capped.

        For a file too large to hold: every other verb reads the body whole behind ``max_response_bytes``. The chunks
        are the body after content-decoding, so the caller sees the same bytes a buffered ``get`` would. The call runs
        through the same stack (retry, pacing, breaker) and the same failure ladder, with these differences:

        * a 2xx is the only success; there is no ``expect=``, so no status is a declared answer, and a 3xx raises
          unless redirects are followed;
        * the client's ``refusal=`` check runs on the response head, before the first chunk, so a host that answers a
          missing file with a 200 HTML page is refused instead of streamed;
        * a failure while the body is arriving (a dropped connection, a read timeout) raises ``TransportError`` or
          ``ClientTimeoutError`` from inside the iteration, after earlier chunks were yielded. **A retry covers only
          the head**; resuming a half-read body is the caller's call, and it must discard what it kept.

        The request goes out when iteration starts, not when this is called.
        """
        if isinstance(url, Template):
            url = render_path(url)
        log_url = sanitize_url(url)
        source = type(self).__name__
        if self._refusal is not None and follow_redirects is True:
            raise ValueError(f"{source} has a refusal= check, so no call may follow redirects")
        try:
            request = self._http_client.build_request("GET", url)
            request.extensions[FOLLOW_REDIRECTS_EXT] = (
                self._http_client.follow_redirects if follow_redirects is None else follow_redirects
            )
        except httpx2.InvalidURL as exc:
            raise ClientError(f"invalid URL {log_url}: {exc}", source=source) from exc
        try:
            with self._breakers[request.url.host].track() as verdict:
                try:
                    streamed = await self._http_client.send(
                        request,
                        stream=True,
                        follow_redirects=httpx2.USE_CLIENT_DEFAULT if follow_redirects is None else follow_redirects,
                    )
                    try:
                        if streamed.status_code == 429 or streamed.status_code >= 500:
                            verdict.host_failed()
                            raise transient_status_error(streamed, source=source)
                        refused = self._refusal(streamed) if self._refusal is not None else None
                        if refused is not None:
                            raise refusal_error(
                                refused, await read_head(streamed, _STREAM_ERROR_HEAD_BYTES), log_url, source=source
                            )
                        if not streamed.is_success:
                            raise status_error(
                                await read_head(streamed, _STREAM_ERROR_HEAD_BYTES), log_url, source=source
                            )
                        async for chunk in streamed.aiter_bytes():
                            yield chunk
                    finally:
                        with contextlib.suppress(httpx2.HTTPError):
                            await streamed.aclose()
                except (httpx2.HTTPError, OSError) as exc:
                    if not failed_on_auth_hop(exc):
                        verdict.host_failed()
                    raise transport_error(exc, request.method, log_url, source=source) from exc
        except CircuitOpenError as exc:
            raise TransportError(str(exc), source=source) from None

    # Public API: the verb methods. Each takes the call's declared responses as ``expect=``, either a bare
    # :data:`~headgate._responses.Reader` (the success reader) or an :class:`~headgate._responses.Expect`
    # adding documented non-2xx answers, and returns what the matching reader read. There is no untyped fallback
    # and no overload: an undeclared non-2xx raises, so no call can receive one unread. Request arguments ride
    # :class:`RequestOptions`.

    async def get[T](
        self,
        url: str | Template,
        *,
        expect: Reader[T] | Expect[T],
        retry: bool = True,
        follow_redirects: bool | None = None,
        cache_ttl: float | None = None,
        **options: Unpack[RequestOptions],
    ) -> T:
        """HTTP GET. See :meth:`_request`."""
        return await self._request(
            "GET", url, expect=expect, retry=retry, follow_redirects=follow_redirects, cache_ttl=cache_ttl, **options
        )

    async def delete[T](
        self,
        url: str | Template,
        *,
        expect: Reader[T] | Expect[T],
        retry: bool = True,
        follow_redirects: bool | None = None,
        cache_ttl: float | None = None,
        **options: Unpack[RequestOptions],
    ) -> T:
        """HTTP DELETE. See :meth:`_request`."""
        return await self._request(
            "DELETE", url, expect=expect, retry=retry, follow_redirects=follow_redirects, cache_ttl=cache_ttl, **options
        )

    async def post[T](
        self,
        url: str | Template,
        *,
        expect: Reader[T] | Expect[T],
        retry: bool = True,
        follow_redirects: bool | None = None,
        cache_ttl: float | None = None,
        **options: Unpack[RequestOptions],
    ) -> T:
        """HTTP POST. See :meth:`_request`."""
        return await self._request(
            "POST", url, expect=expect, retry=retry, follow_redirects=follow_redirects, cache_ttl=cache_ttl, **options
        )

    async def put[T](
        self,
        url: str | Template,
        *,
        expect: Reader[T] | Expect[T],
        retry: bool = True,
        follow_redirects: bool | None = None,
        cache_ttl: float | None = None,
        **options: Unpack[RequestOptions],
    ) -> T:
        """HTTP PUT. See :meth:`_request`."""
        return await self._request(
            "PUT", url, expect=expect, retry=retry, follow_redirects=follow_redirects, cache_ttl=cache_ttl, **options
        )

    async def patch[T](
        self,
        url: str | Template,
        *,
        expect: Reader[T] | Expect[T],
        retry: bool = True,
        follow_redirects: bool | None = None,
        cache_ttl: float | None = None,
        **options: Unpack[RequestOptions],
    ) -> T:
        """HTTP PATCH. See :meth:`_request`."""
        return await self._request(
            "PATCH", url, expect=expect, retry=retry, follow_redirects=follow_redirects, cache_ttl=cache_ttl, **options
        )

    async def head[T](
        self,
        url: str | Template,
        *,
        expect: Reader[T] | Expect[T],
        retry: bool = True,
        follow_redirects: bool | None = None,
        cache_ttl: float | None = None,
        **options: Unpack[RequestOptions],
    ) -> T:
        """HTTP HEAD. A HEAD carries no body, so ``expect=nothing`` is its usual declaration. See :meth:`_request`."""
        return await self._request(
            "HEAD", url, expect=expect, retry=retry, follow_redirects=follow_redirects, cache_ttl=cache_ttl, **options
        )

    async def _request[T](
        self,
        method: _Method,
        url: str | Template,
        *,
        expect: Reader[T] | Expect[T],
        retry: bool = True,
        follow_redirects: bool | None = None,
        cache_ttl: float | None = None,
        **options: Unpack[RequestOptions],
    ) -> T:
        """Send an HTTP request and read the reply through the call's declared responses.

        Every response passes four layers, in a fixed order:

        1. **transport** (:meth:`_send`): a transport failure, a 429/5xx that outlasted retries, or an open breaker
           raises;
        2. **host**: the client's ``refusal=`` check runs on every final response; a
           :class:`~headgate._responses.Refused` it returns is raised through
           :func:`~headgate._failures.refusal_error`;
        3. **endpoint**: a status the call declared exactly (``Expect.answers``) goes to its reader. Any other 2xx goes
           to the success reader;
        4. **default**: any other non-2xx raises through :func:`~headgate._failures.status_error`.

        A reader's :class:`~headgate._responses.Refused` becomes :func:`~headgate._failures.refusal_error`
        (its kind, the real status, the provider's code). A reader's ``DecodeError`` on a 2xx becomes
        ``ResponseParseError``. On a declared non-2xx it falls through to the ladder with the real status: a claim
        covers only the body shape it names. There is no ``None`` return except from a reader that returns it
        (``nothing``): every undeclared non-2xx always raises, so no call can receive one unread.

        Request bodies are httpx-native and ride :class:`RequestOptions`: a JSON body is passed as ``json=<builtins>``
        (built at the call site via the flavor's ``to_builtins(req)`` or ``to_builtins(req, drop_none=True)``, which
        apply the model's field renames) — httpx2 serializes it and stamps ``Content-Type: application/json``. A form
        body rides ``data=``. There is no client-side encode seam.

        Per-call overrides: a per-call ``timeout=`` rides :class:`RequestOptions` into the built request;
        ``retry=False`` disables re-sending for this one call; ``follow_redirects=False`` refuses a redirect for this
        one call, which then raises as its 3xx; ``cache_ttl=`` sets this request's cache lifetime. There is no per-call
        rate limiter — pacing is a transport now, and a client that needs a second rate declares it once as a
        ``rate_limits`` path rule.

        **``retry=False`` is the non-idempotent-write opt-out.** ``build_retry`` retries every HTTP method, which is
        right for a data POST and wrong for order placement: a 5xx or dropped connection *after* the upstream accepted
        the order would re-POST it. Passing ``retry=False`` installs a zero-retry policy (one send, no re-send) for
        this request only. It is a per-call FLAG rather than a transport extension so the opt-out is visible in the
        method signature and greppable at the call site.

        ``retry=False`` is the only per-call retry control.

        Per-request auth is injected by the ``httpx2.Auth`` passed as ``auth=`` at construction (see
        :mod:`headgate._auth`). The Auth flow runs inside ``httpx2.AsyncClient`` at send time and uses a setdefault
        merge, so an explicit ``headers=``/``params=`` from the caller still wins a clash.

        A ``Template`` *url* is rendered here — see :func:`headgate._url.path`, which escapes every interpolation
        as one path segment. **This is the single rendering seam**, and the request is built from the rendered string
        once, so everything downstream (the breaker key, the error messages, :func:`~headgate._url.sanitize_url`)
        sees a plain ``str`` or the built request and none of it has to know t-strings exist. A ``str`` *url* is passed
        through verbatim: it is already-trusted structure, which is what lets a paginator forward an absolute cursor URL
        out of a response body without it being escaped into a single segment.
        """
        spec: Expect[T] = (
            expect if isinstance(expect, Expect) else Expect(expect)
        )  # a Reader is a plain function, never an Expect: ty intersects the two
        if isinstance(url, Template):
            url = render_path(url)
        log_url = sanitize_url(url)
        source = type(self).__name__
        if self._refusal is not None and follow_redirects is True:
            raise ValueError(f"{source} has a refusal= check, so no call may follow redirects")
        try:
            # httpx2 types ``params`` as primitive-valued; ours is Mapping[str, object] because call sites pass
            # TypedDicts (see RequestOptions). httpx2 stringifies each value, so this is library friction at one line.
            request = self._http_client.build_request(method, url, **options)  # ty: ignore[invalid-argument-type]  # TypedDict params; see above
            # The three extensions this client adds, set in one place. retry=False is an ASSIGNMENT; so is a per-call
            # TTL, which the cache transport's ``setdefault`` of the client's own TTL then leaves alone; the redirect
            # marker tells drop_followed_redirect_body whether to drop hop bodies.
            if not retry:
                request.extensions["retry"] = _ZERO_RETRY
            if cache_ttl is not None:
                request.extensions["hishel_ttl"] = cache_ttl
            request.extensions[FOLLOW_REDIRECTS_EXT] = (
                self._http_client.follow_redirects if follow_redirects is None else follow_redirects
            )
            resp = await self._send(request, log_url=log_url, follow_redirects=follow_redirects)
        except httpx2.InvalidURL as exc:
            # A URL this client was asked to build (a caller's path, a cursor a provider handed back) or one a redirect
            # joined past httpx2's length limit: not a host failure, so it records nothing on the breaker. InvalidURL is
            # not an HTTPError, so unmapped it would reach the REST boundary as a 500 rather than a 502.
            raise ClientError(f"invalid URL {log_url}: {exc}", source=source) from exc
        if self._refusal is not None and (refused := self._refusal(resp)) is not None:
            raise refusal_error(refused, resp, log_url, source=source)
        read = spec.answers.get(resp.status_code)
        if read is None:
            if not resp.is_success:
                raise status_error(resp, log_url, source=source)
            read = spec.ok
        try:
            return read(resp)
        # The one place a Refused is caught, and the reason it may be caught anywhere: it becomes the ClientError
        # carrying the status the wire sent. Anywhere else a catch would turn a provider's refusal into something other
        # than a ClientError.
        except Refused as refused:
            raise refusal_error(refused, resp, log_url, source=source) from None
        except DecodeError as exc:
            if resp.is_success:
                raise ResponseParseError(f"{log_url}: {exc}", source=source) from exc
            raise status_error(resp, log_url, source=source) from exc
