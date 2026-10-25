# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""The transport stack every client is built on, assembled in one place.

:func:`build_http_client` returns the ``httpx2.AsyncClient`` an ``AsyncBaseClient`` holds, over the stack
:func:`build_transport_stack` builds::

    RetryTransport(                  # re-sends per the RetryPolicy; begins each attempt on the call's context
      AttemptTransport(              # AttemptFinished per send (only with observers)
        MiddlewareTransport(         # HttpOptions.middleware, once per attempt (only with middleware)
          ResilientCacheTransport(   # opt-in response cache; a hit stops here
            RateLimitTransport(      # the rate budget: every network send takes its turn and feeds back
              network_transport)))))  # httpx2.AsyncHTTPTransport(trust_env=False, ...) or HttpOptions.transport

:func:`build_transport_stack` builds the layers bottom-up, one statement per layer: a new layer is one more statement in
its place there, wrapping the layer below it.

The circuit breaker and the deadline are not in this stack. ``AsyncBaseClient`` drives both around the send and the
body read, which no transport can span. The auth flow and the request and response hooks run in ``httpx2.AsyncClient``
above the stack, once per logical call and once per redirect hop.

Nothing here reads the environment: the client and the default transport are built with ``trust_env=False``, and the
cookie jar refuses every cookie unless ``HttpOptions(cookies=True)``.
"""

from __future__ import annotations

from http.cookiejar import CookieJar, DefaultCookiePolicy
from typing import TYPE_CHECKING

import httpx2

from headgate._attempts import AttemptTransport
from headgate._auth import StaticHeaderAuth
from headgate._middleware import MiddlewareTransport
from headgate._options import httpx_timeout
from headgate._redirects import credential_header_guard, drop_followed_redirect_body
from headgate._user_agent import user_agent
from headgate.cache._credentials import credential_scope
from headgate.cache._transport import ResilientCacheTransport
from headgate.ratelimit import PathBudgets, RateLimitTransport
from headgate.retry import RetryTransport

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from headgate._core import Timing
    from headgate._options import HttpOptions, TransportPolicy
    from headgate.ratelimit import Rate, RateBudget

__all__ = ["build_http_client", "build_transport_stack"]


def build_http_client(
    *, base_url: str, auth: httpx2.Auth | None, http: HttpOptions, transport: httpx2.AsyncBaseTransport
) -> httpx2.AsyncClient:
    """Build a client's ``httpx2.AsyncClient`` over *transport*, the stack :func:`build_transport_stack` built."""
    # httpx2 joins a relative path onto base_url only when base_url ends with "/".
    base_url = base_url if base_url.endswith("/") else base_url + "/"
    return httpx2.AsyncClient(
        transport=transport,
        auth=auth,
        base_url=base_url,
        headers=_default_headers(http),
        cookies=None if http.cookies else _refusing_jar(),
        follow_redirects=http.follow_redirects,
        max_redirects=http.max_redirects,
        event_hooks={"request": _request_hooks(auth, httpx2.URL(base_url)), "response": [drop_followed_redirect_body]},
        timeout=httpx_timeout(http.timeout),
        trust_env=False,
    )


def build_transport_stack(
    rate: Rate | RateBudget,
    policy: TransportPolicy,
    http: HttpOptions,
    timing: Timing,
    auth: httpx2.Auth | None = None,
) -> httpx2.AsyncBaseTransport:
    """Build the layers under ``httpx2.AsyncClient``, bottom-up: each statement wraps the layer before it.

    *auth* is the client's, which the cache partitions its entries by.
    """
    layer = _network(http)
    budgets = PathBudgets(default=rate, rules=policy.rate_limits, timing=timing)
    layer = RateLimitTransport(budgets=budgets, transport=layer)
    if (cache := policy.cache) is not None:
        scope = credential_scope(cache, auth)
        layer = ResilientCacheTransport(layer, cache, max_body_bytes=http.max_response_bytes, scope=scope)
    if http.middleware:
        layer = MiddlewareTransport(transport=layer, middleware=http.middleware)
    if http.observers:
        layer = AttemptTransport(transport=layer)
    return RetryTransport(transport=layer, policy=policy.retry, sleep=timing.sleep)


def _network(http: HttpOptions) -> httpx2.AsyncBaseTransport:
    """Return the network transport: the caller's own, used as it is, or an HTTP/2 pool that reads no environment."""
    if http.transport is not None:
        return http.transport
    return httpx2.AsyncHTTPTransport(
        verify=True if http.ssl_context is None else http.ssl_context,
        http2=True,
        limits=http.limits,
        proxy=http.proxy,
        trust_env=False,
    )


def _default_headers(http: HttpOptions) -> httpx2.Headers:
    """Return the headers every request carries: the ``User-Agent``, then the caller's, which win any clash."""
    headers = httpx2.Headers({"User-Agent": user_agent(http.user_agent)})
    # Headers.update replaces case-insensitively, so a caller's "user-agent" replaces headgate's rather than joining it.
    headers.update(http.headers)
    return headers


def _refusing_jar() -> CookieJar:
    """Return a cookie jar whose policy allows no domain, so it stores no cookie and sends none back."""
    return CookieJar(policy=DefaultCookiePolicy(allowed_domains=[]))


def _request_hooks(auth: httpx2.Auth | None, base_url: httpx2.URL) -> list[Callable[[httpx2.Request], Awaitable[None]]]:
    """Return the request hooks: a guard keeping a ``StaticHeaderAuth``'s headers on the client's own origin."""
    if isinstance(auth, StaticHeaderAuth) and auth.header_names:
        return [credential_header_guard(auth, base_url)]
    return []
