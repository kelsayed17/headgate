# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""The transport stack every client is built on, assembled in one place.

:func:`build_http_client` returns the ``httpx2.AsyncClient`` an ``AsyncBaseClient`` holds, over the stack
:func:`build_transport_stack` builds::

    RetryTransport(                  # re-sends per the RetryPolicy; begins each attempt on the call's context
      AttemptTransport(              # AttemptFinished per send (only with observers)
        MiddlewareTransport(         # HttpOptions.middleware, once per attempt, then the credential guard again
          ResilientCacheTransport(   # opt-in response cache; a hit stops here
            RateLimitTransport(      # the rate budget: every network send takes its turn and feeds back
              SendTrackingTransport( # records whether each send connected and how many request bytes it wrote
                network_transport))))))  # httpx2.AsyncHTTPTransport(trust_env=False, ...) or HttpOptions.transport

:func:`build_transport_stack` builds the layers bottom-up, one statement per layer: a new layer is one more statement in
its place there, wrapping the layer below it.

Above the stack, ``httpx2.AsyncClient`` runs the auth flow and the hooks, once per logical call and once per hop of a
redirect or an auth flow:

* request hooks: the credential guard (:class:`~headgate.security.CredentialGuard`, which also runs below the last
  middleware, where what a middleware stamps is visible), then, over a custom network
  transport with ``SecurityOptions(allow_private_hosts=False)``, the SSRF guard's vetting
  (:func:`~headgate.security.address_hook`); the default transport pins vetted addresses itself
  (:class:`~headgate.security.PinnedHTTPTransport`);
* response hooks: the https-downgrade refusal, the refusal to re-send a one-shot body on a redirect, and the drop of a
  followed redirect's body (:mod:`headgate._redirects`).

The circuit breaker and the deadline are not in this stack. ``AsyncBaseClient`` drives both around the send and the
body read, which no transport can span.

Nothing here reads the environment: the client and the default transport are built with ``trust_env=False``, and the
cookie jar refuses every cookie unless ``HttpOptions(cookies=True)``.
"""

from __future__ import annotations

from dataclasses import dataclass
from http.cookiejar import CookieJar, DefaultCookiePolicy
from typing import TYPE_CHECKING

import httpx2

from headgate._attempts import AttemptTransport
from headgate._middleware import MiddlewareTransport
from headgate._options import httpx_timeout
from headgate._redirects import drop_followed_redirect_body, refuse_unreplayable_redirect
from headgate._user_agent import user_agent
from headgate.cache._credentials import credential_scope
from headgate.cache._transport import ResilientCacheTransport
from headgate.ratelimit._budgets import PathBudgets
from headgate.ratelimit._transport import RateLimitTransport
from headgate.retry._tracking import SendTrackingTransport
from headgate.retry._transport import RetryTransport
from headgate.security import AddressGuard, PinnedHTTPTransport
from headgate.security._credentials import CredentialGuard
from headgate.security._downgrade import refuse_https_downgrade
from headgate.security._transport import address_hook

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from headgate._core import Timing
    from headgate._options import HttpOptions, TransportPolicy
    from headgate.ratelimit import Rate, RateBudget

__all__ = ["Credentials", "build_http_client", "build_transport_stack"]

#: The response hooks, in the order they run on every hop.
_RESPONSE_HOOKS = (refuse_https_downgrade, refuse_unreplayable_redirect, drop_followed_redirect_body)


@dataclass(frozen=True, slots=True)
class Credentials:
    """A client's credentials as its stack sees them: the auth, and the one guard that keeps them on trusted origins.

    The same guard runs as the first request hook and below the last middleware (see :func:`build_transport_stack`).
    """

    auth: httpx2.Auth | None
    guard: CredentialGuard

    @classmethod
    def of(cls, *, base_url: str, auth: httpx2.Auth | None, http: HttpOptions) -> Credentials:
        """Return *auth*, with a guard withholding its and *http*'s middleware's credentials from untrusted origins."""
        guard = CredentialGuard(
            auth=auth,
            base_url=httpx2.URL(base_url),
            trusted_origins=http.security.trusted_origins,
            cookie_jar=http.cookies,
            middleware=http.middleware,
        )
        return cls(auth, guard)


def build_http_client(
    *, base_url: str, credentials: Credentials, http: HttpOptions, transport: httpx2.AsyncBaseTransport
) -> httpx2.AsyncClient:
    """Build a client's ``httpx2.AsyncClient`` over *transport*, the stack :func:`build_transport_stack` built."""
    # httpx2 joins a relative path onto base_url only when base_url ends with "/".
    base_url = base_url if base_url.endswith("/") else base_url + "/"
    return httpx2.AsyncClient(
        transport=transport,
        auth=credentials.auth,
        base_url=base_url,
        headers=_default_headers(http),
        cookies=None if http.cookies else _refusing_jar(),
        follow_redirects=http.follow_redirects,
        max_redirects=http.max_redirects,
        event_hooks={
            "request": _request_hooks(credentials.guard, http),
            "response": list(_RESPONSE_HOOKS),
        },
        timeout=httpx_timeout(http.timeout),
        trust_env=False,
    )


def build_transport_stack(
    rate: Rate | RateBudget,
    policy: TransportPolicy,
    http: HttpOptions,
    timing: Timing,
    credentials: Credentials,
) -> httpx2.AsyncBaseTransport:
    """Build the layers under ``httpx2.AsyncClient``, bottom-up: each statement wraps the layer before it.

    The cache partitions its entries by *credentials*' auth, and its guard runs again below the last middleware, so a
    middleware's declared credentials stay on trusted origins too.
    """
    layer = SendTrackingTransport(transport=_network(http))
    budgets = PathBudgets(default=rate, rules=policy.rate_limits, timing=timing)
    layer = RateLimitTransport(budgets=budgets, transport=layer)
    if (cache := policy.cache) is not None:
        scope = credential_scope(cache, credentials.auth)
        layer = ResilientCacheTransport(layer, cache, max_body_bytes=http.max_response_bytes, scope=scope)
    if http.middleware:
        layer = MiddlewareTransport(transport=layer, middleware=http.middleware, guard=credentials.guard)
    if http.observers:
        layer = AttemptTransport(transport=layer)
    return RetryTransport(transport=layer, policy=policy.retry, sleep=timing.sleep, clock=timing.clock)


def _network(http: HttpOptions) -> httpx2.AsyncBaseTransport:
    """Return the network transport: the caller's own, used as it is, or an HTTP/2 pool that reads no environment."""
    if http.transport is not None:
        return http.transport
    guard = _address_guard(http)
    if guard is not None:
        return PinnedHTTPTransport(guard, ssl_context=http.ssl_context, limits=http.limits)
    return httpx2.AsyncHTTPTransport(
        verify=True if http.ssl_context is None else http.ssl_context,
        http2=True,
        limits=http.limits,
        proxy=http.proxy,
        trust_env=False,
    )


def _address_guard(http: HttpOptions) -> AddressGuard | None:
    """Return the SSRF guard *http* asks for, or ``None`` when it allows private hosts."""
    security = http.security
    if security.allow_private_hosts:
        return None
    return AddressGuard() if security.resolver is None else AddressGuard(security.resolver)


def _request_hooks(
    credentials: CredentialGuard, http: HttpOptions
) -> list[Callable[[httpx2.Request], Awaitable[None]]]:
    """Return the request hooks: the credential guard, then the SSRF guard when a custom transport cannot pin."""
    hooks: list[Callable[[httpx2.Request], Awaitable[None]]] = [credentials]
    addresses = _address_guard(http)
    if addresses is not None and http.transport is not None:
        hooks.append(address_hook(addresses))
    return hooks


def _default_headers(http: HttpOptions) -> httpx2.Headers:
    """Return the headers every request carries: the ``User-Agent``, then the caller's, which win any clash."""
    headers = httpx2.Headers({"User-Agent": user_agent(http.user_agent)})
    # Headers.update replaces case-insensitively, so a caller's "user-agent" replaces headgate's rather than joining it.
    headers.update(http.headers)
    return headers


def _refusing_jar() -> CookieJar:
    """Return a cookie jar whose policy allows no domain, so it stores no cookie and sends none back."""
    return CookieJar(policy=DefaultCookiePolicy(allowed_domains=[]))
