# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""The option objects a :class:`~headgate.Client` is configured with, beyond its URL, rate and auth.

* :class:`TransportPolicy`: what the transport stack does for every request (retries, the timeouts and the deadline
  they are checked against, the breaker, per-path rates, the cache, idempotency keys, per-route overrides).
* :class:`HttpOptions`: how a request is sent and its response read (the network transport and its TLS, pool and proxy,
  headers, redirects, the body cap, cookies).
* :class:`Timeouts`, :class:`IdempotencyKeys` and :class:`Route`, the values those hold.

Every one is frozen, slotted and keyword-only, validates on construction so a bad configuration fails where it is
written, and freezes the collections it is given.
"""

from __future__ import annotations

import functools
import math
import ssl
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import TYPE_CHECKING, Final, Unpack, final, override

import httpx2

# A closed TypedDict (PEP 728) refuses a keyword it does not name, so a stray or misspelled option is a type error.
from typing_extensions import TypedDict

from headgate._body import DEFAULT_MAX_RESPONSE_BYTES
from headgate._call_options import check_options
from headgate._core import (
    check_callable,
    check_collection,
    check_fragment,
    check_int,
    check_kind,
    check_names,
    origin_of,
    origin_text,
    path_has_fragment,
)
from headgate._failures import DEFAULT_REQUEST_ID_HEADERS
from headgate.breaker._policy import BreakerPolicy
from headgate.cache._policy import HttpCache
from headgate.ratelimit._budget import RateBudget
from headgate.ratelimit._rate import Rate
from headgate.retry._policy import RetryPolicy

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from headgate._responses import ResponseCheck
    from headgate.security import Resolver

__all__ = [
    "DEFAULT_LIMITS",
    "DEFAULT_TIMEOUTS",
    "KEYED_METHODS",
    "HttpOptions",
    "IdempotencyKeys",
    "Route",
    "RouteOptions",
    "SecurityOptions",
    "Timeouts",
    "TransportPolicy",
    "check_deadline",
    "check_hooks",
    "check_middleware",
    "check_timeout_fits",
    "httpx_timeout",
    "match_route",
    "reject_upstream_httpx",
]

#: The connection pool of the default network transport: 10 connections, 5 kept alive. The rate budget is the
#: throttle; the pool only has to be wide enough never to bind first, and at 300 ms per request 10 connections sustain
#: ~33 req/s. A client budgeted above that passes wider ``limits``.
DEFAULT_LIMITS: Final = httpx2.Limits(max_connections=10, max_keepalive_connections=5)

#: The proxy URL schemes httpx2 can speak (``socks5`` through the ``httpx2[socks]`` extra).
_PROXY_SCHEMES: Final = frozenset({"http", "https", "socks5", "socks5h"})

#: The schemes a trusted origin may name.
_ORIGIN_SCHEMES: Final = frozenset({"http", "https"})


def reject_upstream_httpx(value: object, *, what: str) -> None:
    """Raise ``TypeError`` when *value* comes from upstream ``httpx``, which headgate does not support.

    An ``httpx`` transport, auth or event hook passes no ``isinstance`` check against httpx2's classes but fails later
    and obscurely, so the mistake is named where it is made: by the module of any class in the value's MRO, or of the
    value itself for a function.
    """
    modules = [cls.__module__ for cls in type(value).__mro__]
    modules.append(str(getattr(value, "__module__", "")))
    if any(module == "httpx" or module.startswith("httpx.") for module in modules):
        msg = (
            f"{what} is an upstream httpx object ({type(value).__qualname__}); headgate is built on httpx2, so pass "
            f"the httpx2 equivalent"
        )
        raise TypeError(msg)


def _check_positive(name: str, value: float) -> None:
    """Raise ``ValueError`` unless *value* is a positive number (NaN is not)."""
    if not value > 0:
        msg = f"{name} must be > 0 (got {value})"
        raise ValueError(msg)


def check_deadline(deadline: float | None) -> None:
    """Raise ``ValueError`` unless *deadline* is ``None`` or a positive, finite number of seconds (NaN is not).

    The one check for every place a deadline is set: the policy, a route, a view and a call.

    :raises TypeError: for a *deadline* that is neither a number nor ``None``, a ``bool`` included.
    """
    if isinstance(deadline, bool) or not isinstance(deadline, int | float | None):
        check_kind("deadline", deadline, (), "seconds, or None for no deadline")
    if deadline is not None and not (deadline > 0 and math.isfinite(deadline)):
        msg = f"deadline must be > 0 and finite, or None (got {deadline})"
        raise ValueError(msg)


#: The timeout phases, in the order ``httpx2.Timeout`` names them.
_PHASES: Final = ("connect", "read", "write", "pool")


@dataclass(frozen=True, slots=True, kw_only=True)
class Timeouts:
    """Per-phase timeouts in seconds: obtaining a connection from the pool, connecting, writing, reading.

    A phase left ``None`` takes its value from the layer beneath: a call's from its view's or its client's, and a
    client's from :data:`DEFAULT_TIMEOUTS` (connect 10 s, read 30 s, write 10 s, pool 15 s). So
    ``timeout=Timeouts(read=300.0)`` lengthens the read timeout and keeps every other phase as it was. A phase set to
    ``math.inf`` has no limit of its own: under a deadline it gets the time the deadline leaves, as a phase with no
    limit does in httpx2.
    """

    connect: float | None = None
    read: float | None = None
    write: float | None = None
    pool: float | None = None

    def __post_init__(self) -> None:
        """Refuse a timeout that is not positive."""
        for name, value in _set_phases(self):
            _check_positive(name, value)

    def over(self, base: Timeouts) -> Timeouts:
        """Return *base* with the phases this sets in place of its own."""
        return replace(base, **dict(_set_phases(self)))


def _set_phases(timeouts: Timeouts) -> list[tuple[str, float]]:
    """Return the phases *timeouts* sets, with their timeouts, in ``httpx2.Timeout``'s order."""
    return [(name, value) for name in _PHASES if (value := getattr(timeouts, name)) is not None]


#: The timeouts of every phase a client leaves unset. ``timeout.over(DEFAULT_TIMEOUTS)`` is what *timeout* resolves to
#: on a client, every phase set: what a deadline computed from the timeouts reads.
DEFAULT_TIMEOUTS: Final = Timeouts(connect=10.0, read=30.0, write=10.0, pool=15.0)


#: What a ``timeout=`` takes, for :func:`check_kind`'s message.
_TIMEOUTS_WANTED: Final = "a Timeouts, such as Timeouts(read=5.0)"


def httpx_timeout(client: Timeouts, call: Timeouts | None = None) -> httpx2.Timeout:
    """Return the ``httpx2.Timeout`` for a request: the *call*'s phases over the *client*'s, over the defaults.

    :raises TypeError: for a *call* timeout that is not a :class:`Timeouts`.
    """
    if call is None:
        return _client_timeout(client)
    check_kind("timeout", call, Timeouts, _TIMEOUTS_WANTED)
    return _as_httpx(call.over(client.over(DEFAULT_TIMEOUTS)))


@functools.lru_cache(maxsize=64)
def _client_timeout(client: Timeouts) -> httpx2.Timeout:
    """Return *client*'s ``httpx2.Timeout``, cached: every call without a ``timeout=`` of its own sends it.

    Sharing one is safe, since httpx2 copies the ``Timeout`` it is handed.
    """
    return _as_httpx(client.over(DEFAULT_TIMEOUTS))


def _as_httpx(resolved: Timeouts) -> httpx2.Timeout:
    """Return *resolved*, every phase set, as an ``httpx2.Timeout``, a phase with no limit (``math.inf``) as ``None``.

    Every phase is set here, so ``None`` means no limit to httpx2, never "inherit".
    """
    return httpx2.Timeout(
        connect=_limit(resolved.connect),
        read=_limit(resolved.read),
        write=_limit(resolved.write),
        pool=_limit(resolved.pool),
    )


def _limit(seconds: float | None) -> float | None:
    """Return *seconds* as httpx2 takes a phase's limit: ``None`` for no limit."""
    return None if seconds == math.inf else seconds


def check_timeout_fits(timeout: Timeouts, deadline: float | None) -> None:
    """Refuse a *timeout* someone set that is longer than the *deadline* it runs under, since it could never apply.

    Only the phases *timeout* sets are checked, so a short deadline over the default timeouts stays valid: there the
    deadline cuts each attempt's timeouts, as meant.

    :raises TypeError: for a *timeout* that is not a :class:`Timeouts`.
    :raises ValueError: naming the phase, its timeout and the deadline.
    """
    check_kind("timeout", timeout, Timeouts, _TIMEOUTS_WANTED)
    if deadline is None:
        return
    for name, limit in _set_phases(timeout):
        # No limit under a deadline is the deadline, which cuts it as it cuts a phase left unset.
        if deadline < limit < math.inf:
            msg = (
                f"{name} timeout {limit:g}s is longer than the {deadline:g}s deadline it runs under, so it can never "
                f"take effect: raise the deadline (TransportPolicy(deadline=...) or deadline=), or lower the timeout"
            )
            raise ValueError(msg)


def _uuid4_text() -> str:
    """Return a random UUID as text."""
    return str(uuid.uuid4())


@dataclass(frozen=True, slots=True, kw_only=True)
class IdempotencyKeys:
    """Stamp a fresh idempotency key on every POST and PATCH a client sends, once per call, under ``header``.

    The key is stamped before the retry layer, so every attempt of one call carries the same key and the upstream can
    recognize a re-send. A call can pass its own with ``idempotency_key=``, and a key the caller set in ``headers=``
    is kept.
    """

    header: str = "Idempotency-Key"
    factory: Callable[[], str] = field(default=_uuid4_text)

    def __post_init__(self) -> None:
        """Refuse a header that is not a name, and a factory that cannot be called."""
        check_kind("header", self.header, str, "a header name")
        check_callable("factory", self.factory, "a function returning a fresh key")
        if not self.header:
            msg = "header must name a header"
            raise ValueError(msg)


#: The methods :class:`IdempotencyKeys` stamps: the ones RFC 9110 does not define as idempotent.
KEYED_METHODS: Final = frozenset({"POST", "PATCH"})


class RouteOptions(TypedDict, total=False, closed=True):
    """What a :class:`Route` sets for its paths; an option left out takes the policy's, and ``None`` is off.

    * ``retry``: a :class:`~headgate.RetryPolicy` that replaces the policy's on these paths, or ``None`` to send once.
    * ``deadline``: seconds a call on these paths may take in all, or ``None`` for no deadline.
    * ``rate``: a :class:`~headgate.Rate`, from which the client builds a budget of these paths' own, a
      :class:`~headgate.RateBudget` shared with whoever else holds it, or ``None`` to pace nothing of their own (the
      server's pauses still apply). Left out, the paths spend from the client's budget.
    * ``cache``: ``None`` keeps these paths out of the client's cache: neither looked up nor stored. Left out, the
      cache's own rules apply.
    """

    retry: RetryPolicy | None
    deadline: float | None
    rate: Rate | RateBudget | None
    cache: None


#: The keywords :class:`RouteOptions` names, read once.
_ROUTE_KEYS: Final = frozenset(RouteOptions.__optional_keys__)


@final
class Route:
    """The one per-path rule: what the calls whose URL path holds ``fragment``'s segments do differently.

    ``Route(fragment, /, **options)`` takes :class:`RouteOptions`. The fragment is one or more whole path segments,
    matched anywhere in the path (``"/exports"`` matches ``/v2/exports/orders.csv``, not ``/v2/exports-archive``), as
    the cache's path filter matches. The first route whose fragment matches a call's path is that call's route,
    whole: what it sets applies, and what it leaves out comes from the policy and the client's budget. Two routes
    never combine. A call's own options, and its view's, win over its route's. A redirect's later hops keep the
    call's route.

    A route is a value: two with the same fragment and options are equal, a shared ``RateBudget`` compared by
    identity.

    :raises TypeError: for an option :class:`RouteOptions` does not name, and for a value of the wrong kind, such as
        ``retry=False`` (write ``retry=None``).
    :raises ValueError: for a fragment naming no segment, which would match every path, and for a deadline that is not
        positive and finite.
    """

    __slots__ = ("_fragment", "_options")

    def __init__(self, fragment: str, /, **options: Unpack[RouteOptions]) -> None:
        """Hold *fragment* and the *options* its paths take."""
        check_kind("fragment", fragment, str, "path segments, such as '/exports'")
        check_fragment(fragment)
        check_options("Route", options, _ROUTE_KEYS)
        if "retry" in options:
            retry = options["retry"]
            if isinstance(retry, bool):
                msg = "retry= takes a RetryPolicy, or None to send once (got bool): write retry=None for retry=False"
                raise TypeError(msg)
            check_kind("retry", retry, (RetryPolicy, type(None)), "a RetryPolicy, or None to send once")
        if "deadline" in options:
            check_deadline(options["deadline"])
        if "rate" in options:
            check_kind("rate", options["rate"], (Rate, RateBudget, type(None)), "a Rate, a RateBudget or None")
        if "cache" in options and options["cache"] is not None:
            check_kind("cache", options["cache"], (), "None, to keep these paths out of the cache")
        self._fragment = fragment
        self._options = options.copy()

    @property
    def fragment(self) -> str:
        """The path segments the route matches."""
        return self._fragment

    @property
    def options(self) -> RouteOptions:
        """A copy of what the route sets: an option it leaves out is not a key."""
        return self._options.copy()

    @override
    def __eq__(self, other: object) -> bool:
        return isinstance(other, Route) and (self._fragment, self._options) == (other._fragment, other._options)

    @override
    def __hash__(self) -> int:
        return hash((self._fragment, tuple(sorted(self._options.items()))))

    @override
    def __repr__(self) -> str:
        options = "".join(f", {name}={value!r}" for name, value in self._options.items())
        return f"Route({self._fragment!r}{options})"


def match_route(routes: tuple[Route, ...], path: str) -> Route | None:
    """Return the first of *routes* whose fragment *path* holds, or ``None``."""
    return next((route for route in routes if path_has_fragment(path, route.fragment)), None)


@dataclass(frozen=True, slots=True, kw_only=True)
class TransportPolicy:
    """What the transport stack does for every request.

    * ``retry``: how a call is re-sent (:class:`~headgate.RetryPolicy`), or ``None`` to send every call once. A call
      opts out with ``retry=None``, and a call's or a route's policy replaces this one.
    * ``timeout``: :class:`Timeouts`; a phase it leaves unset takes its default (:data:`DEFAULT_TIMEOUTS`). A phase it
      sets may not be longer than ``deadline``, since it could never take effect.
    * ``deadline``: seconds the whole logical call may take, every attempt, wait and the body read included; ``None``
      for none. Past it the call raises ``ClientTimeoutError``. A call overrides it with ``deadline=``.
    * ``breaker``: the circuit breaker (:class:`~headgate.BreakerPolicy`), or ``None`` for none.
    * ``cache``: an opt-in :class:`~headgate.HttpCache`.
    * ``idempotency``: stamp idempotency keys on POST and PATCH (:class:`IdempotencyKeys`), or ``None``.
    * ``routes``: the per-path rules (:class:`Route`): a call's first matching route sets its retry, deadline, rate and
      cache, and what it leaves out comes from here.
    """

    retry: RetryPolicy | None = field(default_factory=RetryPolicy)
    timeout: Timeouts = field(default_factory=Timeouts)
    deadline: float | None = 60.0
    breaker: BreakerPolicy | None = field(default_factory=BreakerPolicy)
    cache: HttpCache | None = None
    idempotency: IdempotencyKeys | None = None
    routes: tuple[Route, ...] = ()

    def __post_init__(self) -> None:
        """Refuse a retry or breaker of the wrong kind and a deadline that is not positive and finite; freeze the rules.

        :raises TypeError: for a ``retry`` that is neither a :class:`~headgate.RetryPolicy` nor ``None`` (a ``bool``
            included), or a ``breaker`` that is neither a :class:`~headgate.BreakerPolicy` nor ``None``.
        """
        check_kind("retry", self.retry, (RetryPolicy, type(None)), "a RetryPolicy, or None to send every call once")
        check_kind("breaker", self.breaker, (BreakerPolicy, type(None)), "a BreakerPolicy, or None for no breaker")
        check_kind("cache", self.cache, (HttpCache, type(None)), "an HttpCache, or None for no cache")
        check_kind("idempotency", self.idempotency, (IdempotencyKeys, type(None)), "IdempotencyKeys(), or None")
        check_deadline(self.deadline)
        check_timeout_fits(self.timeout, self.deadline)
        wanted = "a tuple of Route, such as (Route('/exports', deadline=300.0),)"
        if isinstance(self.routes, Route):
            check_kind("routes", self.routes, (), wanted)
        check_collection("routes", self.routes, wanted)
        routes = tuple(self.routes)
        for route in routes:
            check_kind("routes", route, Route, wanted)
        object.__setattr__(self, "routes", routes)


def _is_bare_origin(url: httpx2.URL) -> bool:
    """Return whether *url* is an http(s) origin and nothing more: a host, no path, query, fragment or userinfo."""
    return (
        url.scheme in _ORIGIN_SCHEMES
        and bool(url.host)
        and url.path == "/"
        and not (url.query or url.fragment or url.userinfo)
    )


def _normalized_origin(text: str) -> str:
    """Return *text*, an ``http(s)://host[:port]`` origin, normalized; ``ValueError`` for anything else."""
    try:
        url = httpx2.URL(text)
    except httpx2.InvalidURL:
        url = None
    if url is None or not _is_bare_origin(url):
        msg = f"trusted origin must be an http(s) origin such as 'https://host[:port]' (got {text!r})"
        raise ValueError(msg)
    return origin_text(origin_of(url))


#: What a proxy URL's error calls a URL httpx2 cannot parse, or one with no scheme.
_MALFORMED = "malformed"


def _check_proxy(proxy: str) -> None:
    """Raise ``ValueError`` for a proxy URL httpx2 cannot use."""
    try:
        scheme = httpx2.URL(proxy).scheme or _MALFORMED
    except httpx2.InvalidURL:
        scheme = _MALFORMED
    if scheme not in _PROXY_SCHEMES:
        msg = f"proxy must be an http, https or socks5 URL (got a {scheme} URL)"
        raise ValueError(msg)


def check_hooks(name: str, values: Iterable[object]) -> None:
    """Refuse an upstream-httpx object among *values*, a client's ``observers`` or ``middleware``."""
    for value in values:
        reject_upstream_httpx(value, what=f"an entry of {name}")


def check_middleware(middleware: Iterable[object]) -> None:
    """Refuse a middleware that does not declare the credentials it stamps as two frozensets."""
    for item in middleware:
        declared = (getattr(item, "credential_headers", None), getattr(item, "credential_params", None))
        if not all(isinstance(names, frozenset) and all(isinstance(n, str) for n in names) for names in declared):
            name = getattr(item, "__qualname__", type(item).__qualname__)
            msg = (
                f"middleware {name} must declare credential_headers and credential_params as frozensets of names, "
                "empty when it stamps none"
            )
            raise TypeError(msg)


@dataclass(frozen=True, slots=True, kw_only=True)
class SecurityOptions:
    """Where a client's requests and credentials may go, passed as ``HttpOptions(security=...)``.

    * ``trusted_origins``: origins besides ``base_url``'s that credentials may be sent to (``"https://host[:port]"``);
      every other origin gets the request without them (see :mod:`headgate.security`).
    * ``block_private_hosts``: ``True`` turns on the SSRF guard: a request to a host that is, or resolves to, an
      address that is not globally routable raises :class:`~headgate.errors.ForbiddenDestinationError`, on every hop,
      and the default transport connects only to the address it checked. It cannot be combined with
      ``HttpOptions.proxy``, since a proxy resolves the target itself; over a custom ``HttpOptions.transport`` every hop
      is checked but not pinned.
    * ``resolver``: how the SSRF guard resolves names (:data:`~headgate.security.Resolver`), the event loop's
      ``getaddrinfo`` by default; only with ``block_private_hosts=True``.
    """

    trusted_origins: frozenset[str] = frozenset()
    block_private_hosts: bool = False
    resolver: Resolver | None = None

    def __post_init__(self) -> None:
        """Refuse a value of the wrong kind and a resolver without the SSRF guard, and normalize the trusted origins."""
        # Each origin may be text or an httpx2.URL; _normalized_origin refuses anything else.
        check_collection(
            "trusted_origins", self.trusted_origins, "a frozenset of origins, such as {'https://cdn.example'}"
        )
        check_kind("block_private_hosts", self.block_private_hosts, bool, "a bool")
        if self.resolver is not None:
            check_callable("resolver", self.resolver, "a resolver function, or None")
        if not self.block_private_hosts and self.resolver is not None:
            msg = "resolver= is used only by the SSRF guard, so it needs block_private_hosts=True"
            raise ValueError(msg)
        object.__setattr__(self, "trusted_origins", frozenset(map(_normalized_origin, self.trusted_origins)))


@dataclass(frozen=True, slots=True, kw_only=True)
class HttpOptions:
    """How a request is sent and its response read.

    * ``transport``: a custom network transport (``headgate.curl.AsyncCurlTransport``, a test double), which then owns
      its own TLS, pool and proxy: ``ssl_context``, ``limits`` and ``proxy`` configure only the default transport, an
      HTTP/2 ``httpx2.AsyncHTTPTransport``.
    * ``headers``: sent on every request to ``base_url``'s origin and the ``trusted_origins``; any other origin gets
      only ``User-Agent`` and the ``Accept`` headers among them. A call's ``headers=`` wins a clash.
    * ``follow_redirects`` and ``max_redirects``: the client's default, which a call can override, and the hop limit.
      httpx2 counts an auth flow's hops in the same history as redirects, so each request a
      :class:`~headgate.CachedTokenAuth` mint sends, and a 401 it re-mints after, uses one of ``max_redirects`` too.
    * ``max_response_bytes``: the cap on a buffered body, counted after decompression.
    * ``response_check``: a response check run on every final response (see :data:`~headgate.ResponseCheck`). It sees
      only the final response, so a refusal written on a followed hop would be skipped, and it therefore requires
      ``follow_redirects=False``.
    * ``security``: where requests and credentials may go (:class:`SecurityOptions`): trusted origins and the SSRF
      guard.
    * ``user_agent``: a product token sent before headgate's own (``"<user_agent> headgate/<v> httpx2/<v>
      python/<v>"``); without one, headgate's alone.
    * ``cookies``: ``True`` keeps httpx2's cookie jar; by default no cookie a response sets is stored or sent back.
    * ``request_id_headers``: the response headers a ``ClientError``'s ``request_id`` is read from, first match wins.

    The timeouts are the policy's (``TransportPolicy(timeout=)``), and the observers and middleware the client's own
    (``Client(observers=, middleware=)``): parts of the stack, not settings of how a request is sent.

    Nothing is read from the environment: no proxy variable, no ``.netrc``, no ``SSL_CERT_FILE``.
    """

    transport: httpx2.AsyncBaseTransport | None = None
    headers: Mapping[str, str] = field(default_factory=dict)
    follow_redirects: bool = True
    max_redirects: int = 5
    max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES
    response_check: ResponseCheck | None = None
    security: SecurityOptions = field(default_factory=SecurityOptions)
    ssl_context: ssl.SSLContext | None = None
    limits: httpx2.Limits = field(default_factory=lambda: DEFAULT_LIMITS)
    proxy: str | None = None
    user_agent: str | None = None
    cookies: bool = False
    request_id_headers: tuple[str, ...] = DEFAULT_REQUEST_ID_HEADERS

    def __post_init__(self) -> None:
        """Validate every option, and freeze and normalize the collections."""
        self._check_kinds()
        self._check_transport()
        _check_positive("max_response_bytes", self.max_response_bytes)
        if self.max_redirects < 0:
            msg = f"max_redirects must be >= 0 (got {self.max_redirects})"
            raise ValueError(msg)
        self._check_guard()
        if self.response_check is not None and self.follow_redirects:
            msg = "an HttpOptions with a response_check= must set follow_redirects=False"
            raise ValueError(msg)
        if self.user_agent is not None and (not self.user_agent or not self.user_agent.isprintable()):
            msg = "user_agent must be a non-empty, printable product token"
            raise ValueError(msg)
        names = tuple(name.lower() for name in self.request_id_headers)
        if not all(names):
            msg = "request_id_headers must name headers"
            raise ValueError(msg)
        object.__setattr__(self, "headers", MappingProxyType(dict(self.headers)))
        object.__setattr__(self, "request_id_headers", names)

    def _check_kinds(self) -> None:
        """Refuse a value of the wrong kind where it is written, rather than at the first request."""
        if self.transport is not None and not isinstance(self.transport, httpx2.AsyncBaseTransport):
            reject_upstream_httpx(self.transport, what="transport")
            check_kind("transport", self.transport, (), "an httpx2.AsyncBaseTransport, or None")
        check_kind("headers", self.headers, Mapping, "a mapping of header names to values")
        check_kind("follow_redirects", self.follow_redirects, bool, "a bool")
        check_int("max_redirects", self.max_redirects, "a number of hops, an int")
        check_int("max_response_bytes", self.max_response_bytes, "a number of bytes, an int")
        if self.response_check is not None:
            check_callable("response_check", self.response_check, "a function of the response, or None")
        check_kind("security", self.security, SecurityOptions, "SecurityOptions(...)")
        check_kind("ssl_context", self.ssl_context, (ssl.SSLContext, type(None)), "an ssl.SSLContext, or None")
        check_kind("limits", self.limits, httpx2.Limits, "httpx2.Limits(...)")
        check_kind("proxy", self.proxy, (str, type(None)), "a proxy URL, or None")
        check_kind("user_agent", self.user_agent, (str, type(None)), "a product token, or None")
        check_kind("cookies", self.cookies, bool, "a bool")
        check_names("request_id_headers", self.request_id_headers, "a tuple of header names, such as ('x-trace-id',)")

    def _check_guard(self) -> None:
        """Refuse the SSRF guard with a proxy, which would resolve the target past it."""
        if self.security.block_private_hosts and self.proxy is not None:
            msg = "block_private_hosts=True cannot be combined with proxy=: the proxy resolves the target itself"
            raise ValueError(msg)

    def _check_transport(self) -> None:
        """Refuse an upstream-httpx transport, and default-transport settings next to a custom transport."""
        if self.transport is None:
            if self.proxy is not None:
                _check_proxy(self.proxy)
            return
        reject_upstream_httpx(self.transport, what="transport")
        configured = [
            name
            for name, is_set in (
                ("ssl_context", self.ssl_context is not None),
                ("limits", self.limits != DEFAULT_LIMITS),
                ("proxy", self.proxy is not None),
            )
            if is_set
        ]
        if configured:
            msg = f"{', '.join(configured)} configure the default transport, so they cannot be combined with transport="
            raise ValueError(msg)
