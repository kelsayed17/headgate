# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""Provider-agnostic ``httpx2.Auth`` classes: static headers, query parameters, and a cached minted token.

``httpx2.AsyncClient`` runs ``auth.async_auth_flow(request)`` once per logical request, above the transport stack, so a
re-send by the retry transport repeats the already-signed request. Only a re-send the flow itself yields signs again:
the single 401 re-mint in :class:`CachedTokenAuth`. An auth that needs a provider's own signer (a JWT signed with a
vendor key) belongs in that provider's client.

**An explicit ``headers=`` or ``params=`` from the caller wins a clash.** Every class here merges setdefault-style,
because the flow runs at send time and a plain assignment would override the caller.

Every class here is a :class:`~headgate._core.CredentialStamper`: it declares the headers and query parameters it
stamps (``credential_headers``, ``credential_params``), so the client withholds them from any origin it does not trust
(:mod:`headgate.security`), and it names the credential it carries (``credential_identity()``), so a cache can keep one
credential's responses from another's. The identity is the secret material itself; it is hashed, never logged or
stored.

Only the async flow is implemented; the clients are async-only.
"""

from __future__ import annotations

import asyncio
import math
import secrets
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, override

import httpx2

from headgate._core import origin_text
from headgate._failures import mint_error
from headgate._url import register_secret_params
from headgate.errors import ClientConfigurationError
from headgate.security import CHAIN_ORIGIN

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Awaitable, Iterable, Mapping

__all__ = [
    "AUTH_HOP",
    "FRESH_TOKEN_S",
    "CachedTokenAuth",
    "Minted",
    "QueryParamAuth",
    "StaticHeaderAuth",
    "failed_on_auth_hop",
]

#: The request extension marking a request an auth flow yields to acquire a credential, as opposed to the call's own
#: request. :class:`CachedTokenAuth` sets it on every request its :meth:`~CachedTokenAuth._mint_flow` yields. A fixture
#: recorder can read it to skip the hop, which is not the endpoint's answer.
AUTH_HOP = "headgate.auth_hop"

#: A token rejected within this many seconds of being minted is not re-minted: it is not stale, so a fresh one carries
#: the same authority and is rejected the same way. See :class:`CachedTokenAuth`.
FRESH_TOKEN_S = 60.0


def _identity(pairs: Iterable[tuple[str, str]]) -> bytes:
    """Encode name-value *pairs* as unambiguous bytes, in sorted order: each part length-prefixed, as a netstring."""
    encoded = bytearray()
    for part in (part for pair in sorted(pairs) for part in pair):
        raw = part.encode()
        encoded += b"%d:%b," % (len(raw), raw)
    return bytes(encoded)


def failed_on_auth_hop(exc: BaseException) -> bool:
    """Return whether *exc* was raised by a request an auth flow yielded (see :data:`AUTH_HOP`), not the call's own.

    httpx2 stamps the failing request on every ``RequestError`` it raises, so the marker survives to the catch site. The
    circuit breaker uses this to charge such a failure to nobody: a credential host being unreachable says nothing
    about the host the call was addressed to.
    """
    if not isinstance(exc, httpx2.RequestError):
        return False
    try:
        request = exc.request
    except RuntimeError:  # httpx2 raises when the error was built without a request, as a transport double may do
        return False
    return bool(request.extensions.get(AUTH_HOP))


class StaticHeaderAuth(httpx2.Auth):
    """Stamp a fixed set of headers onto every request, without overriding a header the caller set.

    Covers the static API key and static bearer cases (an ``Authorization: Bearer`` or ``x-api-key`` header). An empty
    mapping is a no-op, so a builder with no key configured returns ``StaticHeaderAuth({})`` rather than ``None``.
    """

    def __init__(self, headers: Mapping[str, str]) -> None:
        """Copy *headers*, so a later change to the caller's mapping does not reach the auth."""
        self._headers = dict(headers)

    @property
    def credential_headers(self) -> frozenset[str]:
        """The headers this auth stamps, lower-cased: withheld from any origin the client does not trust."""
        return frozenset(name.lower() for name in self._headers)

    @property
    def credential_params(self) -> frozenset[str]:
        """No query parameters: this auth stamps headers only."""
        return frozenset[str]()

    def credential_identity(self) -> bytes:
        """Return the headers and their values as bytes, names lower-cased: the same headers give the same bytes."""
        return _identity((name.lower(), value) for name, value in self._headers.items())

    @override
    async def async_auth_flow(self, request: httpx2.Request) -> AsyncGenerator[httpx2.Request, httpx2.Response]:
        for key, value in self._headers.items():
            request.headers.setdefault(key, value)
        yield request


class QueryParamAuth(httpx2.Auth):
    """Merge a fixed set of query params into the request URL, without overriding a param the caller set.

    **The key rides the URL, and URLs are not secret to observability.** A tracing integration that records the full
    request URL (OpenTelemetry's httpx instrumentation, for one) records the key in cleartext, so exclude the host of
    any client using this class from URL capture. Error messages and the HTTP cache are safe regardless: the
    well-known secret parameter names and the names given here are redacted from both. The names are registered
    process-wide, which only ever over-redacts.
    """

    def __init__(self, params: Mapping[str, str]) -> None:
        """Copy *params* and register their names as secret, for redaction from error messages and the cache."""
        self._params = dict(params)
        register_secret_params(self._params)

    @property
    def credential_headers(self) -> frozenset[str]:
        """No headers: this auth stamps query parameters only."""
        return frozenset[str]()

    @property
    def credential_params(self) -> frozenset[str]:
        """The query parameters this auth stamps: withheld from any origin the client does not trust."""
        return frozenset(self._params)

    def credential_identity(self) -> bytes:
        """Return the parameters and their values as bytes: the same parameters give the same bytes."""
        return _identity(self._params.items())

    @override
    async def async_auth_flow(self, request: httpx2.Request) -> AsyncGenerator[httpx2.Request, httpx2.Response]:
        existing = request.url.params
        additions = {k: v for k, v in self._params.items() if k not in existing}
        if additions:
            request.url = request.url.copy_merge_params(additions)
        yield request


@dataclass(frozen=True, slots=True)
class Minted[TokenT]:
    """The last step of a mint flow: the credential, and how long the provider says it lives (seconds)."""

    token: TokenT
    lifetime: float


class CachedTokenAuth[TokenT = str](httpx2.Auth):
    """Bearer auth for providers that mint a short-lived token over the network.

    The token comes from an endpoint, so it is cached, refreshed before it expires, and refreshed again if it is
    rejected early. A subclass gets three behaviours:

    * **A lock-guarded mint.** httpx2 runs requests concurrently, so an unguarded refresh would let N in-flight
      requests each fire their own token exchange.
    * **An expiry margin.** The token is re-minted ``refresh_margin_s`` before it expires (at half-life when the
      lifetime is shorter than twice the margin), because a token that expires mid-flight fails a request that may be
      a write that cannot be repeated.
    * **One forced re-mint on a 401**, never a loop, for a token revoked server-side before its stated lifetime, and
      none for a token minted in the last :data:`FRESH_TOKEN_S` seconds. A 401 on a token that new is a permissions
      problem, not staleness, and re-minting cannot fix it: a gateway that rejects valid tokens (a revoked scope, a
      changed entitlement) costs at most one mint per window, and the 401 reaches the caller as the ``AuthError`` it
      is.

    The exchange itself is the subclass's, because that is where providers differ (form-encoded or JSON, a separate
    sign-in host, their own field names). It supplies one of:

    * :meth:`_mint_token`, a coroutine that makes the exchange on a client of its own and returns
      ``(token, lifetime)``. The usual shape.
    * :meth:`_mint_flow`, an async generator that yields the requests the exchange needs, so they travel through the
      owning client's own stack (its transport, rate budget, retries and error mapping), marked :data:`AUTH_HOP`. It
      ends by yielding :class:`Minted`. A cookie-plus-crumb exchange has this shape.

    Supplying neither fails when the class is defined; with both, the flow runs :meth:`_mint_flow`. Two more hooks let a
    credential that is not an ``Authorization`` bearer ride the same cache: :meth:`_attach` (how a token is put on a
    request) and :meth:`_skips` (a request that needs none). ``TokenT`` is the credential's type, ``str`` for every
    bearer. :meth:`seed` caches a token obtained elsewhere.

    **What it stamps is declared.** :attr:`token_headers` names the headers :meth:`_attach` sets (``Authorization`` by
    default), and is what :attr:`credential_headers` reports, so the client withholds exactly those from an untrusted
    origin. A subclass that overrides :meth:`_attach` must declare its own :attr:`token_headers` (or override
    :attr:`credential_headers`), or the class definition fails: an undeclared header would follow a request anywhere.

    **Its identity is the long-lived credential, not the token.** :meth:`credential_identity` names what the minted
    tokens stand for, so a cache keeps one credential's responses apart from another's across token refreshes. The base
    class cannot see the credential a subclass mints with, so by default each instance is its own identity (random
    bytes drawn once): never shared, which is safe, but two instances holding the same credential share nothing either.
    A subclass that wants them to overrides :meth:`credential_identity` to return its long-lived secret material (the
    API key, or client id and secret), never the short-lived token.

    The hops a :meth:`_mint_flow` yields are addressed by the subclass's own code, so each may carry credentials to the
    origin it names, and the redirects it is answered with stay pinned to that origin (see :mod:`headgate.security`).

    **Every hop counts toward** ``HttpOptions.max_redirects``. httpx2 keeps one history per logical request, and puts
    each response an auth flow answers with in it beside the redirects, so a mint of *n* hops (and the response a 401
    re-mint answers) uses *n* of the call's redirect allowance. A client whose mint makes several hops, or whose
    endpoints redirect too, raises ``max_redirects`` to cover both; past it the call raises ``TransportError``
    ("Exceeded maximum allowed redirects").

    The lock is held while a mint flow's requests are in flight, so requests that need a token wait for the mint to
    finish or fail, bounded by the hop's own timeout. Closing the flow (an error, or the caller's cancellation)
    releases it.

    **A failed mint is reported as the token endpoint's failure**, never the API call's (see
    :func:`~headgate._failures.mint_error`): an exception the mint code raises (a ``ConnectError`` on its own client,
    a ``raise_for_status()``, an unreadable token response) becomes the ``ClientError`` it means, named for this auth
    class and the token endpoint, chained to the original. It is raised from the auth flow, so the API host's circuit
    breaker records nothing for it. A hop :meth:`_mint_flow` yields travels the owning client's stack and fails as any
    request there does.
    """

    #: The headers :meth:`_attach` sets. A subclass whose :meth:`_attach` sets others declares them here.
    token_headers: ClassVar[frozenset[str]] = frozenset({"Authorization"})

    def __init__(self, *, refresh_margin_s: float = 120.0) -> None:
        """Start with an empty cache that refreshes *refresh_margin_s* seconds before a token expires."""
        self._identity = secrets.token_bytes(32)
        self._refresh_margin_s = refresh_margin_s
        #: The margin applied to the cached token: never more than half its lifetime.
        self._margin_s = refresh_margin_s
        self._token: TokenT | None = None
        #: ``time.monotonic()`` deadlines, immune to wall-clock steps. ``-inf`` mint time means the age is unknown.
        self._expires_at = 0.0
        self._minted_at = -math.inf
        self._lock = asyncio.Lock()

    # -- Subclass contract ---------------------------------------------------

    def __init_subclass__(cls, **kwargs: object) -> None:
        """Refuse a subclass with no mint, or whose :meth:`_attach` sets headers it has not declared.

        A subclass must inherit a :meth:`_mint_token` or a :meth:`_mint_flow` override; and when it overrides
        :meth:`_attach`, that class or one below it must declare :attr:`token_headers` or override
        :attr:`credential_headers`.
        """
        super().__init_subclass__(**kwargs)
        # Not @abstractmethod: either of two methods satisfies the contract.
        supplied = any(
            getattr(cls, name) is not getattr(CachedTokenAuth, name) for name in ("_mint_token", "_mint_flow")
        )
        if not supplied:
            msg = f"{cls.__name__} must override _mint_token or _mint_flow"
            raise TypeError(msg)
        mro = cls.__mro__
        attaching = next(klass for klass in mro if "_attach" in vars(klass))
        declared = any(
            "token_headers" in vars(klass) or "credential_headers" in vars(klass)
            for klass in mro[: mro.index(attaching) + 1]
        )
        if not declared:
            msg = (
                f"{cls.__name__} overrides _attach, so it must declare the headers _attach sets in token_headers "
                f"(or override credential_headers)"
            )
            raise TypeError(msg)

    # -- What it stamps (CredentialStamper) ----------------------------------

    @property
    def credential_headers(self) -> frozenset[str]:
        """The headers :meth:`_attach` sets (:attr:`token_headers`), lower-cased."""
        return frozenset(name.lower() for name in self.token_headers)

    @property
    def credential_params(self) -> frozenset[str]:
        """No query parameters: a minted token rides a header."""
        return frozenset[str]()

    def credential_identity(self) -> bytes:
        """Return bytes naming the long-lived credential this auth mints with: by default, this instance's own.

        A subclass whose instances should share a cache partition when they hold the same credential returns that
        credential's secret material here, never the short-lived token, which changes on every refresh.
        """
        return self._identity

    async def _mint_token(self) -> tuple[TokenT, float]:
        """Exchange the provider's long-lived credential for ``(access_token, lifetime_seconds)``.

        Called with the lock held, so an implementation need not guard against concurrent mints. Raise rather than
        return an empty token. An implementation whose provider may rotate its long-lived credential calls
        :meth:`reject_credential_rotation` with the returned value.
        """
        msg = f"{type(self).__name__} mints through _mint_flow, not _mint_token"
        raise NotImplementedError(msg)

    async def _mint_flow(
        self, request: httpx2.Request
    ) -> AsyncGenerator[httpx2.Request | Minted[TokenT], httpx2.Response]:
        """Mint through the owning client's stack: yield each request the exchange needs, end with :class:`Minted`.

        *request* is the call that needs the credential, for a hop to copy its extensions from (timeout, retry
        policy). Called with the lock held, like :meth:`_mint_token`; the default delegates to it.
        """
        del request  # only a flow that yields its own requests needs the call it is minting for
        token, lifetime = await self._mint_token()
        yield Minted(token, lifetime)

    def _attach(self, request: httpx2.Request, token: TokenT) -> None:
        """Put *token* on *request*, replacing any earlier one: the re-send after a 401 calls this a second time.

        An override declares the headers it sets in :attr:`token_headers`.
        """
        request.headers["Authorization"] = f"Bearer {token}"

    def _skips(self, request: httpx2.Request) -> bool:
        """Return whether *request* needs no credential from this auth: by default, one carrying a token header."""
        return any(name in request.headers for name in self.token_headers)

    @staticmethod
    def reject_credential_rotation(returned: str | None, configured: str, *, provider: str, setting: str) -> None:
        """Raise ``ClientConfigurationError`` when a token endpoint returns a replacement for the configured credential.

        When a token endpoint returns a replacement for the credential used to call it, the configured one is dead and
        the replacement exists only in this response. An in-memory cache cannot persist it, so the only safe answer is
        to fail on the first exchange and say why: discarding the replacement silently buys one working call, then an
        opaque ``400`` once the access token expires. An absent or empty *returned*, or one equal to *configured*, is
        benign and common. *setting* names where the operator configured the credential, for the error message.
        """
        if returned is None or returned in {"", configured}:
            return
        msg = (
            f"{provider} returned a new long-lived credential, which means this key is configured for rotating "
            f"credentials. This client cannot use that: the replacement would have to be persisted durably on every "
            f"exchange, and the configured {setting} is now dead. Re-authorise with a non-rotating key and set "
            f"{setting} to the new value."
        )
        raise ClientConfigurationError(msg, source=provider)

    # -- Cache ---------------------------------------------------------------

    def seed(self, token: TokenT, lifetime: float) -> None:
        """Cache *token*, obtained outside this auth, with *lifetime* seconds left to live.

        For a token restored from storage, or a test's fixed bearer. Its age is unknown, so it never counts as fresh:
        a 401 on it re-mints.
        """
        self._cache(token, lifetime, minted_at=-math.inf)

    def _cache(self, token: TokenT, lifetime: float, *, minted_at: float) -> None:
        self._token = token
        self._minted_at = minted_at
        self._expires_at = time.monotonic() + lifetime
        # A lifetime at or under the margin would read as due the moment it was cached, so every request would mint
        # again: refresh at half-life instead.
        self._margin_s = min(self._refresh_margin_s, lifetime / 2)

    def _usable(self, rejected: TokenT | None) -> TokenT | None:
        """Return the cached token unless it is absent, the one a 401 just rejected, or within the refresh margin."""
        cached = self._token
        if cached is not None and cached != rejected and time.monotonic() < self._expires_at - self._margin_s:
            return cached
        return None

    def _is_fresh(self, token: TokenT) -> bool:
        """Return whether *token* is still the cached one and was minted within :data:`FRESH_TOKEN_S`.

        A rejected token that is no longer cached was already replaced by another request, so the replacement is worth
        a retry, and the cache hands it over without minting.
        """
        return token == self._token and time.monotonic() - self._minted_at < FRESH_TOKEN_S

    async def _mint_step(self, step: Awaitable[httpx2.Request | Minted[TokenT]]) -> httpx2.Request | Minted[TokenT]:
        """Await the mint's next *step*, raising a failure in the mint code as the ``ClientError`` it means."""
        try:
            return await step
        except Exception as exc:
            mapped = mint_error(exc, source=type(self).__name__)
            if mapped is exc:
                raise
            raise mapped from exc

    @override
    async def async_auth_flow(self, request: httpx2.Request) -> AsyncGenerator[httpx2.Request, httpx2.Response]:
        if self._skips(request):
            yield request
            return

        # The 401 path re-sends this request, so a streamed body is buffered before the first send. httpx2 honours
        # ``requires_request_body`` only in its default flows, which this method replaces.
        await request.aread()
        # The token a 401 came back for, rather than a "force" flag: each waiter on the lock re-checks the cache
        # against the token it was rejected for, so concurrent 401s share the first replacement instead of each minting.
        rejected: TokenT | None = None
        while True:
            async with self._lock:
                token = self._usable(rejected)
                if token is None:
                    # Only this generator can yield to the client, so it drives the mint flow: httpx2 sends each
                    # request the mint yields and hands its response back in.
                    mint = self._mint_flow(request)
                    try:
                        step = await self._mint_step(anext(mint))
                        while isinstance(step, httpx2.Request):
                            chain = origin_text(step.url.origin)
                            step.extensions = {**step.extensions, AUTH_HOP: True, CHAIN_ORIGIN: chain}
                            hop_response = yield step
                            step = await self._mint_step(mint.asend(hop_response))
                    except StopAsyncIteration:
                        msg = f"{type(self).__name__}._mint_flow ended without yielding Minted"
                        raise RuntimeError(msg) from None
                    finally:
                        await mint.aclose()
                    token = step.token
                    self._cache(token, step.lifetime, minted_at=time.monotonic())
            self._attach(request, token)
            response = yield request
            # ``rejected`` is set only after the first 401, so this re-sends at most once.
            if response.status_code != httpx2.codes.UNAUTHORIZED or rejected is not None or self._is_fresh(token):
                return
            rejected = token
