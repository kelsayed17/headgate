# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""The transport stack every client is built on, assembled in one place.

:func:`build_http_client` returns the ``httpx2.AsyncClient`` an ``AsyncBaseClient`` holds, built over::

    RetryTransport(                  # status/exception retries, backoff, Retry-After
      ResilientCacheTransport(       # opt-in response cache; a hit stops here
        RateLimitTransport(          # the rate budget: every network send takes a token
          base_or_custom_transport)))

The circuit breaker is not in this stack. ``AsyncBaseClient`` drives it around both the send and the body read, which
no transport can span (see :mod:`headgate._breaker`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx2

from headgate._auth import StaticHeaderAuth
from headgate._cache import ResilientCacheTransport
from headgate._rate_limit import PathLimiters, RateLimitTransport
from headgate._redirects import credential_header_guard, drop_followed_redirect_body
from headgate._retry import RetryTransport, build_retry

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from headgate._options import HttpOptions, TransportPolicy
    from headgate._rate_limit import Rate

__all__ = ["build_http_client"]

#: The connection pool every client's default transport gets. The rate limiter is the intended throttle; the pool only
#: has to be wide enough never to bind first (see ``AsyncBaseClient``). A client that needs a different pool passes its
#: own ``HttpOptions(transport=...)``, which owns its pool.
_MAX_CONNECTIONS = 10
_MAX_KEEPALIVE_CONNECTIONS = 5

#: The timeouts other than the read timeout, which is ``HttpOptions.timeout``.
_CONNECT_TIMEOUT = 10.0
_WRITE_TIMEOUT = 10.0
_POOL_TIMEOUT = 15.0


def build_http_client(
    *, base_url: str, rate: Rate, auth: httpx2.Auth | None, policy: TransportPolicy, http: HttpOptions
) -> httpx2.AsyncClient:
    """Build a client's ``httpx2.AsyncClient`` over its policy stack (see the module docstring for the layers)."""
    # httpx2 joins a relative path onto base_url only when base_url ends with "/".
    base_url = base_url if base_url.endswith("/") else base_url + "/"
    return httpx2.AsyncClient(
        transport=RetryTransport(
            transport=_policy_transport(rate, policy, http), retry=build_retry(policy.max_attempts)
        ),
        auth=auth,
        base_url=base_url,
        headers=http.headers,
        follow_redirects=http.follow_redirects,
        event_hooks={"request": _request_hooks(auth, httpx2.URL(base_url)), "response": [drop_followed_redirect_body]},
        timeout=httpx2.Timeout(connect=_CONNECT_TIMEOUT, read=http.timeout, write=_WRITE_TIMEOUT, pool=_POOL_TIMEOUT),
    )


def _policy_transport(rate: Rate, policy: TransportPolicy, http: HttpOptions) -> httpx2.AsyncBaseTransport:
    """Return the layers under retry: the optional cache over the rate limiter over the network transport."""
    # A custom transport (AsyncCurlTransport, a test double) is used as-is and owns its pool.
    network = http.transport or httpx2.AsyncHTTPTransport(
        http2=True,
        limits=httpx2.Limits(max_connections=_MAX_CONNECTIONS, max_keepalive_connections=_MAX_KEEPALIVE_CONNECTIONS),
    )
    paced = RateLimitTransport(limiters=PathLimiters(default=rate, rules=policy.rate_limits), transport=network)
    cache = policy.cache
    if cache is None or cache.storage is None:
        return paced
    return ResilientCacheTransport(
        next_transport=paced,
        storage=cache.storage,
        policy=cache.policy,
        max_body_bytes=http.max_response_bytes,
        ttl=cache.ttl,
    )


def _request_hooks(auth: httpx2.Auth | None, base_url: httpx2.URL) -> list[Callable[[httpx2.Request], Awaitable[None]]]:
    """Return the request hooks: a guard keeping a ``StaticHeaderAuth``'s headers on the client's own origin."""
    if isinstance(auth, StaticHeaderAuth) and auth.header_names:
        return [credential_header_guard(auth, base_url)]
    return []
