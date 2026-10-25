# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""The option objects an :class:`~headgate.AsyncBaseClient` is configured with, beyond its URL, rate and auth.

* :class:`TransportPolicy`: what the transport stack does for every request (retries, the deadline, the breaker,
  per-path rates, the cache, idempotency keys, per-route overrides).
* :class:`HttpOptions`: how a request is sent and its response read (the network transport and its TLS, pool and proxy,
  headers, timeouts, redirects, the body cap, cookies, observers, middleware).
* :class:`Timeouts`, :class:`IdempotencyKeys` and :class:`Route`, the values those hold.

Every one is frozen, slotted and keyword-only, validates on construction so a bad configuration fails where it is
written, and freezes the collections it is given.
"""

from __future__ import annotations

import math
import uuid
from dataclasses import KW_ONLY, dataclass, field, replace
from types import MappingProxyType
from typing import TYPE_CHECKING, Final

import httpx2

from headgate._body import DEFAULT_MAX_RESPONSE_BYTES
from headgate._core import origin_of, origin_text
from headgate._failures import DEFAULT_REQUEST_ID_HEADERS
from headgate.breaker import BreakerPolicy
from headgate.retry import RetryPolicy

if TYPE_CHECKING:
    import ssl
    from collections.abc import Callable, Iterable, Mapping

    from headgate._cache import HttpCache
    from headgate._core import Observer
    from headgate._middleware import Middleware
    from headgate._responses import RefusalCheck
    from headgate.ratelimit import RateRule

__all__ = [
    "DEFAULT_LIMITS",
    "KEYED_METHODS",
    "HttpOptions",
    "IdempotencyKeys",
    "Route",
    "Timeouts",
    "TransportPolicy",
    "httpx_timeout",
    "match_route",
    "reject_upstream_httpx",
]

#: The connection pool of the default network transport: 10 connections, 5 kept alive. The rate limiter is the
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


@dataclass(frozen=True, slots=True, kw_only=True)
class Timeouts:
    """Per-phase timeouts in seconds: obtaining a connection from the pool, connecting, writing, reading."""

    connect: float = 10.0
    read: float = 30.0
    write: float = 10.0
    pool: float = 15.0

    def __post_init__(self) -> None:
        """Refuse a timeout that is not positive."""
        for name in ("connect", "read", "write", "pool"):
            _check_positive(name, getattr(self, name))


def httpx_timeout(client: Timeouts | float, call: Timeouts | float | None = None) -> httpx2.Timeout:
    """Return the ``httpx2.Timeout`` for a request: the *client*'s timeouts with the *call*'s applied.

    A bare number is a read timeout: on the client it keeps the other defaults, on a call the client's other values.
    A ``Timeouts`` replaces all four.
    """
    base = client if isinstance(client, Timeouts) else Timeouts(read=client)
    if call is not None:
        base = call if isinstance(call, Timeouts) else replace(base, read=call)
    return httpx2.Timeout(connect=base.connect, read=base.read, write=base.write, pool=base.pool)


def _uuid4_text() -> str:
    """Return a random UUID as text."""
    return str(uuid.uuid4())


@dataclass(frozen=True, slots=True, kw_only=True)
class IdempotencyKeys:
    """Stamp a fresh idempotency key on every POST and PATCH a client sends, once per call, under ``header``.

    The key is stamped before the retry layer, so every attempt of one call carries the same key and the upstream can
    recognise a re-send. A call can pass its own with ``idempotency_key=``, and a key the caller set in ``headers=``
    is kept.
    """

    header: str = "Idempotency-Key"
    factory: Callable[[], str] = field(default=_uuid4_text)

    def __post_init__(self) -> None:
        """Refuse an empty header name."""
        if not self.header:
            msg = "header must name a header"
            raise ValueError(msg)


#: The methods :class:`IdempotencyKeys` stamps: the ones RFC 9110 does not define as idempotent.
KEYED_METHODS: Final = frozenset({"POST", "PATCH"})


@dataclass(frozen=True, slots=True)
class Route:
    """Overrides for the requests whose URL path contains ``fragment``: their own retry policy and deadline.

    Matched like :class:`~headgate.RateRule`, the first matching route winning, so an endpoint's retry and deadline
    change in the client's configuration rather than in the endpoint method. ``None`` keeps the policy's own value; a
    call's ``retry=`` and ``deadline=`` win over both.
    """

    fragment: str
    _: KW_ONLY
    retry: RetryPolicy | None = None
    deadline: float | None = None

    def __post_init__(self) -> None:
        """Refuse an empty fragment, which would match every path, and a deadline that is not positive."""
        if not self.fragment:
            msg = "fragment must not be empty"
            raise ValueError(msg)
        if self.deadline is not None:
            _check_positive("deadline", self.deadline)


def match_route(routes: tuple[Route, ...], path: str) -> Route | None:
    """Return the first of *routes* whose fragment occurs in *path*, or ``None``."""
    return next((route for route in routes if route.fragment in path), None)


@dataclass(frozen=True, slots=True, kw_only=True)
class TransportPolicy:
    """What the transport stack does for every request.

    * ``retry``: how a call is re-sent (:class:`~headgate.RetryPolicy`). A call opts out with ``retry=False``.
    * ``deadline``: seconds the whole logical call may take, every attempt, wait and the body read included; ``None``
      for none. Past it the call raises ``ClientTimeoutError``. A call overrides it with ``deadline=``.
    * ``breaker``: the circuit breaker (:class:`~headgate.BreakerPolicy`), or ``None`` for none.
    * ``rate_limits``: a second rate for the requests whose path matches a rule (see :class:`~headgate.RateRule`).
    * ``cache``: an opt-in :class:`~headgate.HttpCache`.
    * ``idempotency``: stamp idempotency keys on POST and PATCH (:class:`IdempotencyKeys`), or ``None``.
    * ``routes``: per-route retry and deadline overrides (:class:`Route`).
    """

    retry: RetryPolicy = field(default_factory=RetryPolicy)
    deadline: float | None = 60.0
    breaker: BreakerPolicy | None = field(default_factory=BreakerPolicy)
    rate_limits: tuple[RateRule, ...] = ()
    cache: HttpCache | None = None
    idempotency: IdempotencyKeys | None = None
    routes: tuple[Route, ...] = ()

    def __post_init__(self) -> None:
        """Refuse a deadline that is not positive and finite, and freeze the rule and route sequences."""
        if self.deadline is not None and not (self.deadline > 0 and math.isfinite(self.deadline)):
            msg = f"deadline must be > 0 and finite, or None (got {self.deadline})"
            raise ValueError(msg)
        object.__setattr__(self, "rate_limits", tuple(self.rate_limits))
        object.__setattr__(self, "routes", tuple(self.routes))


def _is_bare_origin(url: httpx2.URL) -> bool:
    """Return whether *url* is an http(s) origin and nothing more: a host, no path, query, fragment or userinfo."""
    return (
        url.scheme in _ORIGIN_SCHEMES
        and bool(url.host)
        and url.path in {"", "/"}
        and not (url.query or url.fragment or url.userinfo)
    )


def _normalized_origin(text: str) -> str:
    """Return *text*, an ``http(s)://host[:port]`` origin, normalised; ``ValueError`` for anything else."""
    try:
        url = httpx2.URL(text)
    except httpx2.InvalidURL:
        url = None
    if url is None or not _is_bare_origin(url):
        msg = f"trusted origin must be an http(s) origin such as 'https://host[:port]' (got {text!r})"
        raise ValueError(msg)
    return origin_text(origin_of(url))


def _check_proxy(proxy: str) -> None:
    """Raise ``ValueError`` for a proxy URL httpx2 cannot use."""
    try:
        scheme = httpx2.URL(proxy).scheme
    except httpx2.InvalidURL:
        scheme = ""
    if scheme not in _PROXY_SCHEMES:
        msg = f"proxy must be an http, https or socks5 URL (got a {scheme or 'malformed'} URL)"
        raise ValueError(msg)


def _check_hooks(name: str, values: Iterable[object]) -> None:
    """Refuse an upstream-httpx object among *values*."""
    for value in values:
        reject_upstream_httpx(value, what=f"an entry of {name}")


@dataclass(frozen=True, slots=True, kw_only=True)
class HttpOptions:
    """How a request is sent and its response read.

    * ``transport``: a custom network transport (``headgate.curl.AsyncCurlTransport``, a test double), which then owns
      its own TLS, pool and proxy: ``ssl_context``, ``limits`` and ``proxy`` configure only the default transport, an
      HTTP/2 ``httpx2.AsyncHTTPTransport``.
    * ``headers``: sent on every request; a call's ``headers=`` wins a clash.
    * ``timeout``: :class:`Timeouts`, or a number for the read timeout alone.
    * ``follow_redirects`` and ``max_redirects``: the client's default, which a call can override, and the hop limit.
    * ``max_response_bytes``: the cap on a buffered body, counted after decompression.
    * ``refusal_check``: a per-host check run on every final response (see :data:`~headgate.RefusalCheck`). It sees
      only the final response, so a refusal written on a followed hop would be skipped, and it therefore requires
      ``follow_redirects=False``.
    * ``trusted_origins``: origins besides ``base_url``'s that credentials may be sent to.
    * ``allow_private_hosts``: ``False`` refuses requests to non-global addresses.
    * ``user_agent``: a product token sent before headgate's own (``"<user_agent> headgate/<v> httpx2/<v>
      python/<v>"``); without one, headgate's alone.
    * ``cookies``: ``True`` keeps httpx2's cookie jar; by default no cookie a response sets is stored or sent back.
    * ``request_id_headers``: the response headers a ``ClientError``'s ``request_id`` is read from, first match wins.
    * ``observers``: receive the client's events (see :mod:`headgate.events`).
    * ``middleware``: run once per attempt, in order, inside the retry layer (see :class:`~headgate.Middleware`).

    Nothing is read from the environment: no proxy variable, no ``.netrc``, no ``SSL_CERT_FILE``.
    """

    transport: httpx2.AsyncBaseTransport | None = None
    headers: Mapping[str, str] = field(default_factory=dict)
    timeout: Timeouts | float = field(default_factory=Timeouts)
    follow_redirects: bool = True
    max_redirects: int = 5
    max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES
    refusal_check: RefusalCheck | None = None
    trusted_origins: frozenset[str] = frozenset()
    allow_private_hosts: bool = True
    ssl_context: ssl.SSLContext | None = None
    limits: httpx2.Limits = field(default_factory=lambda: DEFAULT_LIMITS)
    proxy: str | None = None
    user_agent: str | None = None
    cookies: bool = False
    request_id_headers: tuple[str, ...] = DEFAULT_REQUEST_ID_HEADERS
    observers: tuple[Observer, ...] = ()
    middleware: tuple[Middleware, ...] = ()

    def __post_init__(self) -> None:
        """Validate every option, and freeze and normalise the collections."""
        self._check_transport()
        if not isinstance(self.timeout, Timeouts):
            _check_positive("timeout", self.timeout)
        _check_positive("max_response_bytes", self.max_response_bytes)
        if self.max_redirects < 0:
            msg = f"max_redirects must be >= 0 (got {self.max_redirects})"
            raise ValueError(msg)
        if self.refusal_check is not None and self.follow_redirects:
            msg = "an HttpOptions with a refusal_check= must set follow_redirects=False"
            raise ValueError(msg)
        if self.user_agent is not None and (not self.user_agent or not self.user_agent.isprintable()):
            msg = "user_agent must be a non-empty, printable product token"
            raise ValueError(msg)
        names = tuple(name.lower() for name in self.request_id_headers)
        if not all(names):
            msg = "request_id_headers must name headers"
            raise ValueError(msg)
        _check_hooks("observers", self.observers)
        _check_hooks("middleware", self.middleware)
        object.__setattr__(self, "headers", MappingProxyType(dict(self.headers)))
        object.__setattr__(self, "trusted_origins", frozenset(map(_normalized_origin, self.trusted_origins)))
        object.__setattr__(self, "request_id_headers", names)
        object.__setattr__(self, "observers", tuple(self.observers))
        object.__setattr__(self, "middleware", tuple(self.middleware))

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
