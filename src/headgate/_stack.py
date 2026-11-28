# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""The transport stack every client is built on, assembled in one place.

:func:`build_http_client` returns the ``httpx2.AsyncClient`` a ``Client`` holds, over the stack
:func:`build_transport_stack` builds::

    RetryTransport(                    # re-sends per the RetryPolicy; begins each attempt on the call's context
      AttemptTransport(                # AttemptFinished per send (only with observers)
        MiddlewareTransport(           # the client's middleware, once per attempt, then the request hooks' checks again
          ResilientCacheTransport(     # opt-in response cache; a hit stops here
            RateLimitTransport(        # the rate budget: every network send takes its turn and feeds back
              MiddlewareTransport(     # a SigningAuth's signature, once per send, then the credential guard again
                SendTrackingTransport( # records that each send reached the network, connected, and the bytes it wrote
                  network_transport)))))))  # httpx2.AsyncHTTPTransport(trust_env=False, ...) or HttpOptions.transport

:func:`build_transport_stack` builds the layers bottom-up, one statement per layer: a new layer is one more statement in
its place there, wrapping the layer below it.

Above the stack, ``httpx2.AsyncClient`` runs the auth flow and the hooks, once per logical call and once per hop of a
redirect or an auth flow. A :class:`~headgate.SigningAuth` flow only marks the request: the layer below the rate budget
signs each send, so a request that waited for its turn is signed when it goes out, not when it joined the queue.

* request hooks: the credential guard (:class:`~headgate.security.CredentialGuard`, which also runs below the last
  middleware and below the signature, where what they stamp is visible), then, over a custom network
  transport or a test's fake network with ``SecurityOptions(block_private_hosts=True)``, the SSRF guard's vetting
  (:func:`~headgate.security.address_hook`), which also runs below the last middleware on an attempt whose origin it
  has not admitted, since a middleware may change the host; the default transport
  (:class:`~headgate.security.PinnedHTTPTransport`) and a transport that takes the guard
  (``headgate.curl.AsyncCurlTransport``) pin vetted addresses themselves, on every send;
* response hooks: the https-downgrade refusal, the refusal to re-send a one-shot body on a redirect, and the drop of a
  followed redirect's body (:mod:`headgate._redirects`).

The circuit breaker and the deadline are not in this stack. ``Client`` drives both around the send and the
body read, which no transport can span.

Nothing here reads the environment: the client and the default transport are built with ``trust_env=False``, and the
cookie jar refuses every cookie unless ``HttpOptions(cookies=True)``.
"""

import sys
from contextvars import ContextVar
from dataclasses import dataclass, field
from http.cookiejar import CookieJar, DefaultCookiePolicy
from typing import TYPE_CHECKING, Final, cast, final

import httpx2

from headgate._attempts import AttemptTransport
from headgate._auth import Auth, SigningAuth
from headgate._core import sealed
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
from headgate.security._addresses import AddressGuard, Resolver, resolve_with_loop
from headgate.security._credentials import CredentialGuard
from headgate.security._downgrade import refuse_https_downgrade
from headgate.security._transport import PinnedHTTPTransport, address_hook

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from headgate._core import Timing
    from headgate._curl import GuardBinding
    from headgate._middleware import Middleware
    from headgate._options import CallPolicy, HttpOptions, Timeouts
    from headgate.ratelimit._budgets import PathBudgets

__all__ = [
    "NETWORK_OVERRIDE",
    "OFFLINE_TRANSPORTS",
    "Credentials",
    "NetworkOverride",
    "UnmatchedRequestError",
    "build_http_client",
    "build_transport_stack",
    "sends_on_the_network",
]


@final
@sealed("catch it, or the AssertionError it is")
class UnmatchedRequestError(AssertionError):
    """A request the fake network did not answer: no route of a :class:`~headgate.testing.FakeTransport` matched it.

    Also raised by a client built outside every :func:`~headgate.testing.fake_network` block that makes a call inside
    one, before anything is sent, since it would send on its own network, not the block's (one on a fake of the testing
    kit's own reaches no network, and sends on it). An ``AssertionError``, not an
    ``httpx2`` error, so the client neither retries it nor maps it onto a ``ClientError``: it reaches the test as the
    failure it is.
    """


@dataclass(frozen=True, slots=True)
class NetworkOverride:
    """The network a client built while :data:`NETWORK_OVERRIDE` is set runs on, in place of its own.

    ``transport`` replaces the network transport, even one the client was given; ``resolver`` replaces the SSRF
    guard's resolver, even one the client was given, so no name is looked up on the real network; and ``clients``
    collects each client whose construction finished, in order.
    """

    transport: httpx2.AsyncBaseTransport
    resolver: Resolver
    clients: list[object] = field(default_factory=list)


#: The network every client built while it is set runs on: :func:`headgate.testing.fake_network` sets it.
NETWORK_OVERRIDE: Final = ContextVar[NetworkOverride | None]("headgate_network_override", default=None)

#: The testing kit's transports, which answer every request themselves, so a client built on one outside every fake
#: network reaches no network and a block lets it send: :mod:`headgate.testing` adds them.
OFFLINE_TRANSPORTS: Final[set[type[httpx2.AsyncBaseTransport]]] = set()

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

    auth: Auth | None
    guard: CredentialGuard
    cookie_jar: CookieJar
    middleware: tuple[Middleware, ...] = ()

    @classmethod
    def of(
        cls, *, base_url: str, auth: Auth | None, http: HttpOptions, middleware: tuple[Middleware, ...]
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
    policy: CallPolicy,
    http: HttpOptions,
    timing: Timing,
    credentials: Credentials,
    *,
    observed: bool,
) -> httpx2.AsyncBaseTransport:
    """Build the layers under ``httpx2.AsyncClient``, bottom-up: each statement wraps the layer before it.

    The rate-limit layer spends from *budgets*. The cache partitions its entries by *credentials*' auth. A
    ``SigningAuth`` signs each send below the rate layer, after its wait, and the middleware layer is built for
    *credentials*' middleware; the guard runs again below each, so what they stamp stays on trusted origins too, and
    below the middleware so does the SSRF guard's vetting where the network transport does not pin, on an attempt whose
    origin it has not admitted, so a host a middleware chose is vetted too. An *observed* client's attempts are
    reported (``AttemptFinished``).
    """
    layer = SendTrackingTransport(transport=_network(http))
    if isinstance(signer := credentials.auth, SigningAuth):
        layer = MiddlewareTransport(
            transport=layer, middleware=(), guards=(credentials.guard,), signer=(signer, timing.wall_clock)
        )
    layer = RateLimitTransport(budgets=budgets, transport=layer)
    if (cache := policy.cache) is not None:
        scope = credential_scope(
            cache,
            credentials.auth,
            middleware=credentials.middleware,
            headers=http.headers,
            guard=(http.security.check, http.security.resolver) if http.security.block_private_hosts else None,
        )
        layer = ResilientCacheTransport(layer, cache, max_body_bytes=http.max_response_bytes, scope=scope)
    if middleware := credentials.middleware:
        layer = MiddlewareTransport(
            transport=layer, middleware=middleware, guards=(credentials.guard,), signer=None, vets=_vetting(http)
        )
    if observed:
        layer = AttemptTransport(transport=layer)
    retry = SEND_ONCE if policy.retry is None else policy.retry
    return RetryTransport(transport=layer, policy=retry, timing=timing)


def _network(http: HttpOptions) -> httpx2.AsyncBaseTransport:
    """Return the network transport: a test's override, the caller's own, or an HTTP/2 pool reading no environment."""
    if (override := NETWORK_OVERRIDE.get()) is not None:
        return override.transport
    if (transport := http.transport) is not None:
        if (binding := _guard_binding(transport)) is None:
            return transport
        binding.bind(_address_guard(http))
        # Clients may share a curl transport and its session: the last of them to close closes it.
        return binding.hold(transport)
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


def sends_on_the_network(transport: httpx2.AsyncBaseTransport) -> bool:
    """Return whether *transport* is one of the real network's: httpx2's own, or the curl transport."""
    return isinstance(transport, httpx2.AsyncHTTPTransport) or _guard_binding(transport) is not None


def _guard_binding(transport: httpx2.AsyncBaseTransport) -> GuardBinding | None:
    """Return what binds *transport* to a client's SSRF guard, for a transport that pins itself; else ``None``.

    Only the curl transport pins itself. Its module needs the curl extra, so it is read only once something imported
    it, which building a curl transport does.
    """
    curl = sys.modules.get("headgate._curl")
    return None if curl is None else cast("GuardBinding | None", curl.guard_binding(transport))


def _address_guard(http: HttpOptions) -> AddressGuard | None:
    """Return the SSRF guard *http* asks for, or ``None`` when it allows private hosts.

    Under a test's fake network, the guard resolves names through the fake, in place of the client's own resolver.
    """
    security = http.security
    if not security.block_private_hosts:
        return None
    if (override := NETWORK_OVERRIDE.get()) is not None:
        resolver = override.resolver
    else:
        resolver = resolve_with_loop if security.resolver is None else security.resolver
    return AddressGuard(resolver, check=security.check)


def _request_hooks(
    credentials: CredentialGuard, http: HttpOptions
) -> list[Callable[[httpx2.Request], Awaitable[None]]]:
    """Return the request hooks: the credential guard, then the SSRF guard when the network transport cannot pin."""
    return [credentials, *_vetting(http)]


def _vetting(http: HttpOptions) -> tuple[Callable[[httpx2.Request], Awaitable[None]], ...]:
    """Return the SSRF guard's vetting of each hop, as a hook, when *http* asks for the guard and nothing pins."""
    addresses = _address_guard(http)
    # A test's network is never bound to the guard (_network returns it as it is), so it never pins, whatever it is.
    unpinned = NETWORK_OVERRIDE.get() is not None or (
        http.transport is not None and _guard_binding(http.transport) is None
    )
    return (address_hook(addresses),) if addresses is not None and unpinned else ()


def _default_headers(http: HttpOptions) -> httpx2.Headers:
    """Return the headers every request carries: the ``User-Agent``, then the caller's, which win any clash."""
    headers = httpx2.Headers({"User-Agent": user_agent(http.user_agent)})
    # Headers.update replaces case-insensitively, so a caller's "user-agent" replaces headgate's rather than joining it.
    headers.update(http.headers)
    return headers


def _refusing_jar() -> CookieJar:
    """Return a cookie jar whose policy allows no domain, so it stores no cookie and sends none back."""
    return CookieJar(policy=DefaultCookiePolicy(allowed_domains=[]))
