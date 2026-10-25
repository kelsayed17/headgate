"""The transport stack every client is built on, assembled in one place.

:func:`build_http_client` returns the ``httpx2.AsyncClient`` an ``AsyncBaseClient`` holds, built over::

    RetryTransport(                  # status/exception retries, backoff, Retry-After
      ResilientCacheTransport(       # opt-in response cache; a hit stops here
        RateLimitTransport(          # the rate budget: every network send takes a token
          base_or_custom_transport)))

The circuit breaker is not in this stack. ``AsyncBaseClient`` drives it around the send AND the body read, which no
transport can span (see :mod:`headgate._breaker`).
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
    from collections.abc import Sequence

    from headgate._cache import HttpCache
    from headgate._rate_limit import Rate, RateRule

__all__ = ["build_http_client"]

#: The connection pool every client's default transport gets. The rate limiter is the intended throttle; the pool only
#: has to be wide enough never to bind first (see ``AsyncBaseClient``). A client that needs a different pool passes its
#: own ``transport=``, which owns its pool.
_MAX_CONNECTIONS = 10
_MAX_KEEPALIVE_CONNECTIONS = 5

#: The timeouts other than the read timeout, which is the client's ``timeout=``.
_CONNECT_TIMEOUT = 10.0
_WRITE_TIMEOUT = 10.0
_POOL_TIMEOUT = 15.0


def build_http_client(
    *,
    base_url: str,
    rate: Rate,
    rate_limits: Sequence[RateRule],
    transport: httpx2.AsyncBaseTransport | None,
    cache: HttpCache | None,
    max_response_bytes: int,
    max_attempts: int,
    auth: httpx2.Auth | None,
    default_headers: dict[str, str] | None,
    follow_redirects: bool,
    timeout: float,
) -> httpx2.AsyncClient:
    """Build a client's ``httpx2.AsyncClient`` over its policy stack (see the module docstring for the layers)."""
    # httpx2 joins relative paths only when base_url ends with "/".
    normalized_base_url = base_url if base_url.endswith("/") else base_url + "/"
    # A custom transport (AsyncCurlTransport, a test double) is used as-is and owns its pool.
    base_transport = transport or httpx2.AsyncHTTPTransport(
        http2=True,
        limits=httpx2.Limits(max_connections=_MAX_CONNECTIONS, max_keepalive_connections=_MAX_KEEPALIVE_CONNECTIONS),
    )
    inner: httpx2.AsyncBaseTransport = RateLimitTransport(
        limiters=PathLimiters(default=rate, rules=rate_limits), transport=base_transport
    )
    # The cache is engaged only when an HttpCache with a storage is passed.
    if cache is not None and cache.storage is not None:
        inner = ResilientCacheTransport(
            next_transport=inner,
            storage=cache.storage,
            policy=cache.policy,
            max_body_bytes=max_response_bytes,
            ttl=cache.ttl,
        )
    # A StaticHeaderAuth's headers never leave this client's origin, redirect or not (see credential_header_guard).
    request_hooks = (
        [credential_header_guard(auth, httpx2.URL(normalized_base_url))]
        if isinstance(auth, StaticHeaderAuth) and auth.header_names
        else []
    )
    return httpx2.AsyncClient(
        transport=RetryTransport(transport=inner, retry=build_retry(max_attempts)),
        auth=auth,
        base_url=normalized_base_url,
        headers=default_headers or {},
        follow_redirects=follow_redirects,
        event_hooks={"request": request_hooks, "response": [drop_followed_redirect_body]},
        timeout=httpx2.Timeout(connect=_CONNECT_TIMEOUT, read=timeout, write=_WRITE_TIMEOUT, pool=_POOL_TIMEOUT),
    )
