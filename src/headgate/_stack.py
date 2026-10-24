"""The transport stack every client is built on, assembled in one place.

:func:`build_http_client` is ``AsyncBaseClient.__init__``'s stack half. It returns the ``httpx2.AsyncClient`` the client
holds, built over::

    RetryTransport(                  # status/exception retries + backoff + Retry-After
      (ResilientCacheTransport(      # opt-in response cache; a HIT stops here
        RateLimitTransport(          # the rate budget: every network send takes a token
          base_or_custom_transport))))

The circuit breaker is not in this stack. ``AsyncBaseClient._send`` drives it around the send AND the body read, which
no transport can span (see :mod:`headgate._async_breaker`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx2

from headgate._auth import StaticHeaderAuth
from headgate._cache import ResilientCacheTransport, _NoCloseStorage
from headgate._redirects import credential_header_guard, drop_followed_redirect_body
from headgate._transports import PathLimiters, RateLimitTransport, RetryTransport, build_retry

if TYPE_CHECKING:
    from collections.abc import Sequence

    from headgate._cache import HttpCache
    from headgate._rate_limiter import Rate
    from headgate._transports import RateRule

__all__ = ["build_http_client"]

#: The connection pool every client's default transport gets. The RATE LIMITER is the intended throttle, tuned per
#: provider; the pool only has to be wide enough never to bind first (see ``AsyncBaseClient``'s docstring). A client
#: that needs a different pool passes its own ``transport=``, which owns its pool.
_MAX_CONNECTIONS = 10
_MAX_KEEPALIVE_CONNECTIONS = 5


def build_http_client(
    *,
    base_url: str,
    rate: Rate,
    rate_limits: Sequence[RateRule],
    transport: httpx2.AsyncBaseTransport | None,
    cache: HttpCache | None,
    max_response_bytes: int,
    max_retries: int,
    auth: httpx2.Auth | None,
    default_headers: dict[str, str] | None,
    follow_redirects: bool,
    timeout: float,
) -> httpx2.AsyncClient:
    """Build a client's ``httpx2.AsyncClient`` over its policy stack (see the module docstring for the layers)."""
    # httpx2 joins relative paths only when base_url ends with "/".
    normalized_base_url = base_url if base_url.endswith("/") else base_url + "/"
    # A custom transport (e.g. AsyncCurlTransport for TLS impersonation) is used as-is and owns its own pool; otherwise build the
    # default AsyncHTTPTransport over the shared pool constants above
    # (_MAX_CONNECTIONS / _MAX_KEEPALIVE_CONNECTIONS): identical for every client.
    base_transport = transport or httpx2.AsyncHTTPTransport(
        http2=True,
        limits=httpx2.Limits(max_connections=_MAX_CONNECTIONS, max_keepalive_connections=_MAX_KEEPALIVE_CONNECTIONS),
    )
    # Opt-in cache transport, base-adjacent (innermost). Engaged ONLY when an HttpCache is passed AND it carries a
    # storage; otherwise the client uses the plain stack and no AsyncCacheTransport is ever constructed. A HIT
    # short-circuits here, below retry (nothing to retry) and without reaching the network, so _send marks it and the
    # breaker records neither a success nor a failure for it; hishel calls the wrapped base_transport only on a MISS.
    # The storage is wrapped in _NoCloseStorage so this client's teardown never closes a storage it shares.
    # ResilientCacheTransport is the plain hishel AsyncCacheTransport plus one guarantee: a STORAGE fault is counted and
    # the request redone uncached, rather than failing a fetch that would otherwise succeed. Genuine httpx2 errors still
    # propagate untouched to the retry/breaker layers above.
    # Rate limiting is the BOTTOM layer: a cache HIT returns above it and spends no token, while every re-send the retry
    # layer makes passes through it and spends its own. A per-client rule picks a different limiter for a matching URL
    # path (see PathLimiters).
    paced = RateLimitTransport(limiters=PathLimiters(default=rate, rules=rate_limits), transport=base_transport)
    inner: httpx2.AsyncBaseTransport = paced
    if cache is not None and cache.storage is not None:
        inner = ResilientCacheTransport(
            next_transport=paced,
            storage=_NoCloseStorage(cache.storage),
            policy=cache.policy,
            max_body_bytes=max_response_bytes,
            ttl=cache.ttl,
        )
    # Policy stack: retry (client-default, baked in here) wraps the (cache+)rate-limit+network transports. A caller
    # turns retry OFF for one call with retry=False on a verb (it rides the request's extensions → RetryTransport);
    # there is no per-call way to ask for a different non-zero count.
    request_hooks = (
        [credential_header_guard(auth, httpx2.URL(normalized_base_url))]
        if isinstance(auth, StaticHeaderAuth) and auth.header_names
        else []
    )
    return httpx2.AsyncClient(
        transport=RetryTransport(transport=inner, retry=build_retry(max_retries)),
        auth=auth,
        base_url=normalized_base_url,
        headers=default_headers or {},
        # A StaticHeaderAuth's headers never leave this client's origin, redirect or not: see
        # credential_header_guard. ``follow_redirects=False`` remains for a client that must not follow at all, such as
        # one whose auth computes its custom headers per request.
        follow_redirects=follow_redirects,
        event_hooks={"request": request_hooks, "response": [drop_followed_redirect_body]},
        timeout=httpx2.Timeout(connect=10.0, read=timeout, write=10.0, pool=15.0),
    )
