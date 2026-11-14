# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""The transport stack every client is built on, assembled in one place.

:func:`build_http_client` returns the ``httpx2.AsyncClient`` a ``Client`` holds, over the stack
:func:`build_transport_stack` builds::

    RetryTransport(                    # re-sends per the RetryPolicy; begins each attempt on the call's context
      AttemptTransport(                # AttemptFinished per send (only with observers)
        MiddlewareTransport(           # the client's middleware, once per attempt, then the credential guard again
          ResilientCacheTransport(     # opt-in response cache; a hit stops here
            RateLimitTransport(        # the rate budget: every network send takes its turn and feeds back
              MiddlewareTransport(     # a SigningAuth's signature, once per send, then the credential guard again
                SendTrackingTransport( # records whether each send connected and how many request bytes it wrote
                  network_transport)))))))  # httpx2.AsyncHTTPTransport(trust_env=False, ...) or HttpOptions.transport

:func:`build_transport_stack` builds the layers bottom-up, one statement per layer: a new layer is one more statement in
its place there, wrapping the layer below it.

Above the stack, ``httpx2.AsyncClient`` runs the auth flow and the hooks, once per logical call and once per hop of a
redirect or an auth flow. A :class:`~headgate.SigningAuth` flow only marks the request: the layer below the rate budget
signs each send, so a request that waited for its turn is signed when it goes out, not when it joined the queue.

* request hooks: the credential guard (:class:`~headgate.security.CredentialGuard`, which also runs below the last
  middleware and below the signature, where what they stamp is visible), then, over a custom network
  transport with ``SecurityOptions(block_private_hosts=True)``, the SSRF guard's vetting
  (:func:`~headgate.security.address_hook`); the default transport pins vetted addresses itself
  (:class:`~headgate.security.PinnedHTTPTransport`);
* response hooks: the https-downgrade refusal, the refusal to re-send a one-shot body on a redirect, and the drop of a
  followed redirect's body (:mod:`headgate._redirects`).

The circuit breaker and the deadline are not in this stack. ``Client`` drives both around the send and the
body read, which no transport can span.

Nothing here reads the environment: the client and the default transport are built with ``trust_env=False``, and the
cookie jar refuses every cookie unless ``HttpOptions(cookies=True)``.
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from http.cookiejar import CookieJar, DefaultCookiePolicy
from typing import TYPE_CHECKING, Final

import httpx2

from headgate._attempts import AttemptTransport
from headgate._auth import SigningAuth
from headgate._middleware import MiddlewareTransport
from headgate._options import httpx_timeout
from headgate._redirects import drop_followed_redirect_body, refuse_unreplayable_redirect
from headgate._user_agent import user_agent
from headgate.cache._credentials import credential_scope, mark_caller_credentials
from headgate.cache._transport import ResilientCacheTransport
from headgate.ratelimit._transport import RateLimitTransport
from headgate.retry._policy import SEND_ONCE
from headgate.retry._tracking import SendTrackingTransport
from headgate.retry._transport import RetryTransport
from headgate.security import AddressGuard, PinnedHTTPTransport
from headgate.security._credentials import CredentialGuard
from headgate.security._downgrade import refuse_https_downgrade
from headgate.security._transport import address_hook

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from headgate._core import Timing
    from headgate._middleware import Middleware
    from headgate._options import HttpOptions, Timeouts, TransportPolicy
    from headgate.ratelimit._budgets import PathBudgets

__all__ = ["NETWORK_OVERRIDE", "Credentials", "build_http_client", "build_transport_stack"]

#: The network transport every client built while it is set sends on, in place of its own:
#: :func:`headgate.testing.fake_network` sets it.
NETWORK_OVERRIDE: Final = ContextVar[httpx2.AsyncBaseTransport | None]("headgate_network_override", default=None)

#: The response hooks, in the order they run on every hop.
_RESPONSE_HOOKS = (refuse_https_downgrade, refuse_unreplayable_redirect, drop_followed_redirect_body)


@dataclass(frozen=True, slots=True)
class Credentials:
    """A client's credentials as its stack sees them: the auth, the guard keeping them on trusted origins, the jar.

    The middleware is here too, since the guard keeps what each declares on trusted origins as well. The same guard
    runs as the first request hook and below the last middleware and a ``SigningAuth``'s signature (see
    :func:`build_transport_stack`). The jar is the client's: one that stores nothing unless
    ``HttpOptions(cookies=True)``, and the guard sends an untrusted origin only that jar's cookies for it.
    """

    auth: httpx2.Auth | None
    guard: CredentialGuard
    cookie_jar: CookieJar
    middleware: tuple[Middleware, ...] = ()

    @classmethod
    def of(
        cls, *, base_url: str, auth: httpx2.Auth | None, http: HttpOptions, middleware: tuple[Middleware, ...]
    ) -> Credentials:
        """Return *auth*, with a guard keeping its, the middleware's and client-wide credentials on trusted origins."""
        # httpx2.Cookies() makes the jar httpx2 would make for itself.
        jar = httpx2.Cookies().jar if http.cookies else _refusing_jar()
        guard = CredentialGuard(
            auth=auth,
            base_url=httpx2.URL(base_url),
            http=http,
            middleware=middleware,
            cookie_jar=jar if http.cookies else None,
        )
        return cls(auth, guard, jar, middleware)


def build_http_client(
    *,
    base_url: str,
    credentials: Credentials,
    http: HttpOptions,
    timeout: Timeouts,
    transport: httpx2.AsyncBaseTransport,
) -> httpx2.AsyncClient:
    """Build a client's ``httpx2.AsyncClient`` over *transport*, the stack :func:`build_transport_stack` built."""
    # httpx2 joins a relative path onto base_url only when base_url ends with "/".
    base_url = base_url if base_url.endswith("/") else base_url + "/"
    return httpx2.AsyncClient(
        transport=transport,
        auth=mark_caller_credentials(credentials.auth),
        base_url=base_url,
        headers=_default_headers(http),
        cookies=credentials.cookie_jar,
        follow_redirects=http.follow_redirects,
        max_redirects=http.max_redirects,
        event_hooks={
            "request": _request_hooks(credentials.guard, http),
            "response": list(_RESPONSE_HOOKS),
        },
        timeout=httpx_timeout(timeout),
        trust_env=False,
    )


def build_transport_stack(
    budgets: PathBudgets,
    policy: TransportPolicy,
    http: HttpOptions,
    timing: Timing,
    credentials: Credentials,
    *,
    observed: bool,
) -> httpx2.AsyncBaseTransport:
    """Build the layers under ``httpx2.AsyncClient``, bottom-up: each statement wraps the layer before it.

    The rate-limit layer spends from *budgets*. The cache partitions its entries by *credentials*' auth. A
    ``SigningAuth`` signs each send below the rate layer, after its wait, and the middleware layer is built for
    *credentials*' middleware; the guard runs again below each, so what they stamp stays on trusted origins too. An
    *observed* client's attempts are reported (``AttemptFinished``).
    """
    layer = SendTrackingTransport(transport=_network(http))
    if isinstance(signer := credentials.auth, SigningAuth):
        layer = MiddlewareTransport(transport=layer, middleware=(), guard=credentials.guard, signer=signer)
    layer = RateLimitTransport(budgets=budgets, transport=layer)
    if (cache := policy.cache) is not None:
        scope = credential_scope(
            cache,
            credentials.auth,
            middleware=credentials.middleware,
            headers=http.headers,
            guarded=http.security.block_private_hosts,
        )
        layer = ResilientCacheTransport(layer, cache, max_body_bytes=http.max_response_bytes, scope=scope)
    if middleware := credentials.middleware:
        layer = MiddlewareTransport(transport=layer, middleware=middleware, guard=credentials.guard, signer=None)
    if observed:
        layer = AttemptTransport(transport=layer)
    retry = SEND_ONCE if policy.retry is None else policy.retry
    return RetryTransport(transport=layer, policy=retry, timing=timing)


def _network(http: HttpOptions) -> httpx2.AsyncBaseTransport:
    """Return the network transport: a test's override, the caller's own, or an HTTP/2 pool reading no environment."""
    if (override := NETWORK_OVERRIDE.get()) is not None:
        return override
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
    if not security.block_private_hosts:
        return None
    return AddressGuard() if security.resolver is None else AddressGuard(security.resolver)


def _request_hooks(
    credentials: CredentialGuard, http: HttpOptions
) -> list[Callable[[httpx2.Request], Awaitable[None]]]:
    """Return the request hooks: the credential guard, then the SSRF guard when a transport not ours cannot pin."""
    hooks: list[Callable[[httpx2.Request], Awaitable[None]]] = [credentials]
    addresses = _address_guard(http)
    unpinned = http.transport is not None or NETWORK_OVERRIDE.get() is not None
    if addresses is not None and unpinned:
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
