"""The base client: :class:`AsyncBaseClient`, a policy layer over ``httpx2.AsyncClient``.

httpx2 does the URL joining, headers, timeouts and connection pooling. This adds retries, rate limiting, a per-host
circuit breaker, a response-size cap, and the mapping of every failure onto the ``ClientError`` tree.

A call runs in three steps:

1. :meth:`AsyncBaseClient._prepare` renders the URL and builds the ``httpx2.Request`` once, stamping the per-call
   extensions (the retry override, a cache TTL, the redirect marker);
2. :meth:`AsyncBaseClient._exchange` sends it under the host's circuit breaker through the transport stack
   (:func:`headgate._stack.build_http_client`) and maps a transport failure or a surviving 429/5xx onto a
   ``ClientError`` (:mod:`headgate._failures`);
3. :meth:`AsyncBaseClient._request` caps the body (:func:`headgate._body.read_capped`) and reads the response
   through the call's declared responses (:mod:`headgate._responses`).
"""

from __future__ import annotations

import contextlib
from string.templatelib import Template
from typing import TYPE_CHECKING, Literal, Self, TypedDict, Unpack

import httpx2

from headgate._auth import failed_on_auth_hop
from headgate._body import DEFAULT_MAX_RESPONSE_BYTES, read_capped, read_head
from headgate._breaker import HOST_BREAKERS, NO_BREAKERS, CircuitOpenError
from headgate._failures import refusal_error, status_error, transient_status_error, transport_error
from headgate._redirects import FOLLOW_REDIRECTS_EXT
from headgate._responses import Expect, Reader, RefusalCheck, Refused
from headgate._retry import ZERO_RETRY
from headgate._stack import build_http_client
from headgate._url import render_path, sanitize_url
from headgate.errors import ClientError, DecodeError, ResponseParseError, TransportError
from headgate.policy import policy_is_disabled

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator, Mapping, Sequence
    from types import TracebackType

    from headgate._breaker import Breakers
    from headgate._cache import HttpCache
    from headgate._rate_limit import Rate, RateRule

__all__ = ["AsyncBaseClient", "RequestOptions"]

#: How much of a refused or failed streamed response is read, for the ``ClientError``'s body snippet.
_STREAM_ERROR_HEAD_BYTES = 4096

type _Method = Literal["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"]


class RequestOptions(TypedDict, total=False):
    """The request arguments a verb forwards to ``httpx2.AsyncClient.build_request``.

    ``params`` is ``Mapping[str, object]`` because clients pass a TypedDict for their query parameters, and a TypedDict
    is assignable to no narrower mapping; httpx2 stringifies each value. A JSON body is ``json=`` (build it with a
    codec flavor's ``to_builtins``), a form body ``data=``.
    """

    params: Mapping[str, object] | None
    headers: Mapping[str, str]
    json: object
    data: Mapping[str, object]
    timeout: float


class AsyncBaseClient:
    """Async base for HTTP API clients. Configured through ``__init__``, which builds the transport stack once.

    **Failure policy.** Every verb takes the call's declared responses as ``expect=`` and returns what the matching
    reader read, or raises a :class:`~headgate.errors.ClientError` subtype:

    * ``TransportError`` / ``ClientTimeoutError``: a network failure or timeout, or an open breaker;
    * ``RateLimitError``: a 429 that survived retries, or a quota refusal a reader found inside a 2xx;
    * ``UpstreamStatusError``: a 5xx that survived retries, a status the call did not declare, or a refusal carrying
      the status it arrived with (401/403 become ``AuthError``);
    * ``ResponseParseError``: a 2xx body the reader could not decode;
    * ``ResponseTooLargeError``: a body past ``max_response_bytes``.

    There is no ``None`` return except from a reader that returns it (:func:`~headgate._responses.nothing`): every
    undeclared non-2xx raises, a 404 included. A non-2xx the provider documents as an answer is declared with
    :meth:`Expect.on <headgate._responses.Expect.on>` and comes back as its own wire type.

    **Per-call overrides.** ``timeout=`` rides :class:`RequestOptions`. ``retry=False`` sends once and never re-sends,
    which is what a non-idempotent write (placing an order) needs, since every method is retried by default.
    ``follow_redirects=False`` makes a redirect raise as its 3xx. ``cache_ttl=`` sets this request's cache lifetime.

    **URLs.** A t-string URL (``t"series/{series_id}"``) is rendered by :func:`headgate._url.path`, which escapes every
    interpolation as one path segment. A ``str`` URL is passed through verbatim, so a paginator can forward an absolute
    cursor URL from a response body.

    **The connection pool** is the same for every client (10 connections, 5 keep-alive). The rate limiter is the
    throttle; the pool only has to be wide enough never to bind first, and at 300 ms per request 10 connections
    sustain ~33 req/s. A client budgeted above that should pass its own ``transport=`` with a wider pool.
    """

    def __init__(
        self,
        *,
        base_url: str,
        rate: Rate,
        max_attempts: int = 3,
        default_headers: dict[str, str] | None = None,
        timeout: float = 30.0,
        rate_limits: Sequence[RateRule] = (),
        transport: httpx2.AsyncBaseTransport | None = None,
        circuit_breaker: bool = True,
        cache: HttpCache | None = None,
        auth: httpx2.Auth | None = None,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
        follow_redirects: bool = True,
        refusal_check: RefusalCheck | None = None,
    ) -> None:
        # Transport policy off (headgate.policy): no retries and no breaker. The rate limiter checks for itself.
        if policy_is_disabled():
            max_attempts, circuit_breaker = 0, False
        # A refusal check sees only the FINAL response, so a client following redirects would skip one written on a
        # hop. The per-call half is in _prepare.
        if refusal_check is not None and follow_redirects:
            raise ValueError(
                f"{type(self).__name__}: a client with a refusal_check= must be built with follow_redirects=False"
            )
        self._source = type(self).__name__
        self._refusal_check = refusal_check
        # NO_BREAKERS never reads the shared registry, so a breaker another client opened cannot refuse this one.
        self._breakers: Breakers = HOST_BREAKERS if circuit_breaker else NO_BREAKERS
        self._max_response_bytes = max_response_bytes
        self._http_client = build_http_client(
            base_url=base_url,
            rate=rate,
            rate_limits=rate_limits,
            transport=transport,
            cache=cache,
            max_response_bytes=max_response_bytes,
            max_attempts=max_attempts,
            auth=auth,
            default_headers=default_headers,
            follow_redirects=follow_redirects,
            timeout=timeout,
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc_value: BaseException | None, traceback: TracebackType | None
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http_client.aclose()

    # Public API: the verbs.

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
        """HTTP GET. See :class:`AsyncBaseClient`."""
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
        """HTTP DELETE. See :class:`AsyncBaseClient`."""
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
        """HTTP POST. See :class:`AsyncBaseClient`."""
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
        """HTTP PUT. See :class:`AsyncBaseClient`."""
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
        """HTTP PATCH. See :class:`AsyncBaseClient`."""
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
        """HTTP HEAD. A HEAD carries no body, so ``expect=nothing`` is its usual declaration."""
        return await self._request(
            "HEAD", url, expect=expect, retry=retry, follow_redirects=follow_redirects, cache_ttl=cache_ttl, **options
        )

    async def stream_get(self, url: str | Template, *, follow_redirects: bool | None = None) -> AsyncIterator[bytes]:
        """HTTP GET whose 2xx body is yielded chunk by chunk, never buffered and never capped.

        For a file too large to hold. The chunks are the body after content-decoding, the same bytes a buffered
        ``get`` would see, and the call runs through the same stack and failure ladder, except that:

        * a 2xx is the only success: there is no ``expect=``, and a 3xx raises unless redirects are followed;
        * the client's ``refusal_check`` runs on the response head, before the first chunk;
        * a failure while the body is arriving raises ``TransportError`` or ``ClientTimeoutError`` from inside the
          iteration, after earlier chunks were yielded. A retry covers only the head.

        The request goes out when iteration starts, not when this is called.
        """
        request, log_url = self._prepare("GET", url, follow_redirects=follow_redirects)
        async with self._exchange(request, log_url, follow_redirects=follow_redirects) as streamed:
            if self._refusal_check is not None and (refused := self._refusal_check(streamed)) is not None:
                head = await read_head(streamed, _STREAM_ERROR_HEAD_BYTES)
                raise refusal_error(refused, head, log_url, source=self._source)
            if not streamed.is_success:
                head = await read_head(streamed, _STREAM_ERROR_HEAD_BYTES)
                raise status_error(head, log_url, source=self._source)
            async for chunk in streamed.aiter_bytes():
                yield chunk

    # The three steps of a call.

    def _prepare(
        self,
        method: _Method,
        url: str | Template,
        *,
        follow_redirects: bool | None,
        retry: bool = True,
        cache_ttl: float | None = None,
        **options: Unpack[RequestOptions],
    ) -> tuple[httpx2.Request, str]:
        """Build the request, and the sanitized URL every error message about it names."""
        if isinstance(url, Template):
            url = render_path(url)
        log_url = sanitize_url(url)
        if self._refusal_check is not None and follow_redirects is True:
            raise ValueError(f"{self._source} has a refusal_check=, so no call may follow redirects")
        try:
            # httpx2 types ``params`` as primitive-valued; ours is Mapping[str, object] (see RequestOptions).
            request = self._http_client.build_request(method, url, **options)  # ty: ignore[invalid-argument-type]
        except httpx2.InvalidURL as exc:
            raise ClientError(f"invalid URL {log_url}: {exc}", source=self._source) from exc
        if not retry:
            request.extensions["retry"] = ZERO_RETRY
        # A per-call TTL; the cache transport's setdefault of the client's own TTL then leaves it alone.
        if cache_ttl is not None:
            request.extensions["hishel_ttl"] = cache_ttl
        # Tells drop_followed_redirect_body whether this request's redirect bodies are being thrown away.
        request.extensions[FOLLOW_REDIRECTS_EXT] = (
            self._http_client.follow_redirects if follow_redirects is None else follow_redirects
        )
        return request, log_url

    @contextlib.asynccontextmanager
    async def _exchange(
        self, request: httpx2.Request, log_url: str, *, follow_redirects: bool | None
    ) -> AsyncGenerator[httpx2.Response]:
        """Send *request* under its host's circuit breaker and yield the streamed response, closing it on the way out.

        The response yielded is anything but a 429 or 5xx, which means the retry transport ran out of attempts and is
        raised here: no call may declare either as an answer. A network failure or timeout (in the send, or in the
        caller's body read inside the block) becomes ``TransportError`` / ``ClientTimeoutError``, and an open breaker
        ``TransportError``.

        The breaker is driven here rather than by a transport layer because this is the only scope that spans the
        send AND the body read. It is keyed on the host the request is addressed to, so a followed redirect's hops are
        charged to that host. A transport failure and a surviving 429/5xx are host failures; a cache hit tested
        nothing; anything else that escapes (an auth flow failing to mint, a body over the cap, a cancellation)
        records nothing (see ``CircuitBreaker.track``).
        """
        try:
            with self._breakers[request.url.host].track() as verdict:
                try:
                    streamed = await self._http_client.send(
                        request,
                        stream=True,
                        follow_redirects=httpx2.USE_CLIENT_DEFAULT if follow_redirects is None else follow_redirects,
                    )
                    try:
                        if streamed.extensions.get("hishel_from_cache"):
                            verdict.mark_untested()
                        if streamed.status_code == 429 or streamed.status_code >= 500:
                            verdict.mark_failed()
                            raise transient_status_error(streamed, source=self._source)
                        yield streamed
                    finally:
                        with contextlib.suppress(httpx2.HTTPError):
                            await streamed.aclose()
                except (httpx2.HTTPError, OSError) as exc:
                    # A hop an auth flow yielded to mint a credential failed: that is the credential host's failure,
                    # not this one's.
                    if not failed_on_auth_hop(exc):
                        verdict.mark_failed()
                    raise transport_error(exc, request.method, log_url, source=self._source) from exc
        except CircuitOpenError as exc:
            raise TransportError(str(exc), source=self._source) from None
        except httpx2.InvalidURL as exc:
            # A redirect joined past httpx2's length limit. Not an HTTPError, and not a host failure.
            raise ClientError(f"invalid URL {log_url}: {exc}", source=self._source) from exc

    async def _request[T](
        self,
        method: _Method,
        url: str | Template,
        *,
        expect: Reader[T] | Expect[T],
        retry: bool,
        follow_redirects: bool | None,
        cache_ttl: float | None,
        **options: Unpack[RequestOptions],
    ) -> T:
        """Send a request and read the reply through the call's declared responses.

        Every response passes four layers, in order:

        1. **transport** (:meth:`_exchange`): a transport failure, a surviving 429/5xx, or an open breaker raises;
        2. **host**: the client's ``refusal_check`` runs on the final response;
        3. **endpoint**: a status the call declared exactly goes to its reader; any other 2xx to the success reader;
        4. **default**: any other non-2xx raises through :func:`~headgate._failures.status_error`.

        A reader's ``Refused`` becomes the ``ClientError`` its kind names, with the real status. A reader's
        ``DecodeError`` on a 2xx becomes ``ResponseParseError``; on a declared non-2xx it falls through to the status
        ladder, because a declaration covers only the body shape it names.
        """
        spec: Expect[T] = expect if isinstance(expect, Expect) else Expect(expect)
        request, log_url = self._prepare(
            method, url, follow_redirects=follow_redirects, retry=retry, cache_ttl=cache_ttl, **options
        )
        async with self._exchange(request, log_url, follow_redirects=follow_redirects) as streamed:
            resp = await read_capped(streamed, self._max_response_bytes, source=self._source, url=log_url)
        if self._refusal_check is not None and (refused := self._refusal_check(resp)) is not None:
            raise refusal_error(refused, resp, log_url, source=self._source)
        read = spec.answers.get(resp.status_code)
        if read is None:
            if not resp.is_success:
                raise status_error(resp, log_url, source=self._source)
            read = spec.ok
        try:
            return read(resp)
        # The one place a Refused is caught: it becomes the ClientError carrying the status the wire sent.
        except Refused as refused:
            raise refusal_error(refused, resp, log_url, source=self._source) from None
        except DecodeError as exc:
            if resp.is_success:
                raise ResponseParseError(f"{log_url}: {exc}", source=self._source) from exc
            raise status_error(resp, log_url, source=self._source) from exc
