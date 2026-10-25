# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""Provider-agnostic ``httpx2.Auth`` classes: static headers, query parameters, and a cached minted token.

``httpx2.AsyncClient`` runs ``auth.async_auth_flow(request)`` once per logical request, ABOVE the transport stack, so
a re-send the retry transport makes re-sends the same, already-signed request. Only a re-send the flow itself yields
signs again: the single 401 re-mint in :class:`CachedTokenAuth`. An auth that needs a provider's own signer (a JWT
signed with a vendor key) belongs in that provider's client.

**An explicit ``headers=`` or ``params=`` from the caller wins a clash.** Every class here merges setdefault-style
(``if key not in request.headers``), because the flow runs at send time and a plain assignment would override the
caller.

Only the async flow is implemented; the clients are async-only.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, override

import httpx2

from headgate._url import register_secret_params
from headgate.errors import ClientConfigurationError

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

__all__ = ["AUTH_HOP", "CachedTokenAuth", "Minted", "QueryParamAuth", "StaticHeaderAuth", "failed_on_auth_hop"]

#: The request extension an :class:`httpx2.Auth` flow sets on a request it YIELDS to acquire a credential (as opposed to
#: the call's own request). A fixture recorder can read it to skip the hop, which is not the endpoint's answer.
AUTH_HOP = "headgate.auth_hop"


def failed_on_auth_hop(exc: BaseException) -> bool:
    """Whether *exc* was raised by a request an auth flow yielded (see :data:`AUTH_HOP`), not the call's own.

    httpx2 stamps the failing request on every ``RequestError`` it raises, so the marker survives to the catch site. The
    circuit breaker uses this to charge such a failure to nobody: a credential host being unreachable says nothing
    about the host the call was addressed to.
    """
    if not isinstance(exc, httpx2.RequestError):
        return False
    try:
        request = exc.request
    except RuntimeError:  # httpx2 raises when the error was built without a request, as a transport double may
        return False
    return bool(request.extensions.get(AUTH_HOP))


#: A token rejected within this many seconds of being minted is NOT re-minted: it is not stale, so a fresh one carries
#: the same authority and will be rejected the same way. See :class:`CachedTokenAuth`.
_FRESH_TOKEN_S = 60.0


class StaticHeaderAuth(httpx2.Auth):
    """Stamp a fixed set of headers onto every request (setdefault merge).

    Covers the static-API-key / static-bearer majority (a bearer token, an ``x-api-key`` header). An empty
    mapping is a no-op — the idiomatic "no key configured" builder result, so callers construct ``StaticHeaderAuth({})``
    rather than branching on ``auth=None``.
    """

    def __init__(self, headers: dict[str, str]) -> None:
        self._headers = dict(headers)

    @property
    def header_names(self) -> frozenset[str]:
        """The headers this auth stamps — what ``AsyncBaseClient`` withholds from any hop that leaves its origin."""
        return frozenset(self._headers)

    @override
    async def async_auth_flow(self, request: httpx2.Request) -> AsyncGenerator[httpx2.Request, httpx2.Response]:
        for key, value in self._headers.items():
            # setdefault: an explicit caller header wins a clash.
            if key not in request.headers:
                request.headers[key] = value
        yield request


class QueryParamAuth(httpx2.Auth):
    """Merge a fixed set of query params into the request URL (setdefault).

    **The key rides the URL, and URLs are not secret to observability.** A tracing integration that records the full
    request URL (OpenTelemetry's httpx instrumentation, for one) will record the key in cleartext, so exclude the host
    of any client using this class from URL capture. Error messages and the HTTP cache are safe regardless: the
    well-known secret parameter names and the names given here are redacted from both.
    """

    def __init__(self, params: dict[str, str]) -> None:
        self._params = dict(params)
        register_secret_params(self._params)

    @override
    async def async_auth_flow(self, request: httpx2.Request) -> AsyncGenerator[httpx2.Request, httpx2.Response]:
        existing = request.url.params
        # setdefault: only add a key the caller didn't already supply, so an
        # explicit ``params={"api_key": …}`` from the call site wins.
        additions = {k: v for k, v in self._params.items() if k not in existing}
        if additions:
            request.url = request.url.copy_merge_params(additions)
        yield request


@dataclass(frozen=True, slots=True)
class Minted[TokenT]:
    """What a mint flow ends with: the credential, and how long the provider says it lives (seconds)."""

    token: TokenT
    lifetime: float


class CachedTokenAuth[TokenT = str](httpx2.Auth):
    """Bearer auth for providers that MINT a short-lived token over the network.

    The token has to be fetched from an endpoint, so it is cached, refreshed before it expires, and refreshed again if
    it is rejected early. A subclass gets three behaviours for free:

    * **lock-guarded mint** — httpx2 runs requests concurrently, so an unguarded refresh lets N in-flight requests each
      fire their own token exchange;
    * **an expiry margin** — re-mint BEFORE expiry, because a token that expires mid-flight fails a request that may be
      a write that cannot be repeated;
    * **one forced re-mint on a 401**, never a loop, for a token revoked server-side before its stated lifetime — and
      **none for a token minted in the last** ``_FRESH_TOKEN_S`` **seconds**. A 401 on a token that new is a
      permissions problem, not staleness, and re-minting cannot fix it. Without that exception a gateway that rejects
      valid tokens (a revoked scope, a changed entitlement) would cost one mint PER REQUEST. Now it costs at most one
      mint per ``_FRESH_TOKEN_S``, and the 401 reaches the caller as the ``AuthError`` it is.

    What is deliberately NOT here: the exchange itself. That is where providers genuinely differ (form-encoded or JSON,
    a separate sign-in host, their own field names). A subclass supplies it in one of two ways:

    * :meth:`_mint_token`, a coroutine that makes the exchange itself (on a client of its own) and returns
      ``(token, lifetime)``. The usual shape.
    * :meth:`_mint_flow`, an async generator that YIELDS the requests the exchange needs, so they travel through the
      owning client's own stack (its transport, rate limiter, retries and error mapping) and are marked
      :data:`AUTH_HOP`. It ends by yielding :class:`Minted`. A cookie-plus-crumb exchange is this shape.

    A subclass supplies at least one (checked when the class is defined). If it somehow has both, the auth flow runs
    :meth:`_mint_flow`; :meth:`_access_token`, which cannot yield, is :meth:`_mint_token`'s alone and raises for a class
    that mints only through a flow.

    Two more hooks are how a credential that is not an ``Authorization`` bearer rides the same cache: :meth:`_attach`
    (how a token is put on a request; the default sets the bearer) and :meth:`_skips` (a request that needs none;
    the default is one already carrying an ``Authorization`` header). ``TokenT`` is the credential's type, ``str`` for
    every bearer.
    """

    def __init__(self, *, refresh_margin_s: float = 120.0) -> None:
        self._refresh_margin_s = refresh_margin_s
        #: The margin actually applied to the cached token: never more than half its lifetime (see _access_token).
        self._margin_s = refresh_margin_s
        self._token: TokenT | None = None
        #: A ``time.monotonic()`` deadline, not a wall clock — immune to NTP steps.
        self._expires_at = 0.0
        #: When the cached token was minted, on the same clock. ``-inf`` until the first mint.
        self._minted_at = float("-inf")
        self._lock = asyncio.Lock()

    # -- Subclass contract ---------------------------------------------------

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        # Not @abstractmethod: either of two methods satisfies the contract, so the class statement checks for one.
        supplied = any(
            getattr(cls, name) is not getattr(CachedTokenAuth, name) for name in ("_mint_token", "_mint_flow")
        )
        if not supplied:
            msg = f"{cls.__name__} must override _mint_token or _mint_flow"
            raise TypeError(msg)

    async def _mint_token(self) -> tuple[TokenT, float]:
        """Exchange the provider's long-lived credential for ``(access_token, lifetime_seconds)``.

        Called with the lock held, so an implementation need not guard against concurrent mints. Should raise rather
        than return an empty token.

        An implementation whose provider may ROTATE its long-lived credential must call
        :meth:`reject_credential_rotation` with the returned value — see that method for why silence is the expensive
        option.
        """
        msg = f"{type(self).__name__} mints through _mint_flow, not _mint_token"
        raise NotImplementedError(msg)

    async def _mint_flow(
        self, request: httpx2.Request
    ) -> AsyncGenerator[httpx2.Request | Minted[TokenT], httpx2.Response]:
        """Mint through the owning client's stack: yield each request the exchange needs, end with :class:`Minted`.

        *request* is the call that needs the credential, for a hop to copy its extensions from (timeout, retry
        policy). Called with the lock held, like :meth:`_mint_token`, and the default delegates to it.
        """
        del request  # only a flow that yields its own requests needs the call it is minting for
        token, lifetime = await self._mint_token()
        yield Minted(token, lifetime)

    def _attach(self, request: httpx2.Request, token: TokenT) -> None:
        """Put *token* on *request*, replacing any earlier one: the retry after a 401 calls this a second time."""
        request.headers["Authorization"] = f"Bearer {token}"

    def _skips(self, request: httpx2.Request) -> bool:
        """Whether *request* needs no credential from this auth. Default: an explicit ``Authorization`` wins."""
        return "Authorization" in request.headers

    @staticmethod
    def reject_credential_rotation(returned: str | None, configured: str, *, provider: str, setting: str) -> None:
        """Refuse a provider that just rotated the long-lived credential out from under us.

        A general OAuth hazard, not one provider's quirk: when a token endpoint returns a REPLACEMENT for the credential
        used to call it, the configured one is now dead and the replacement exists only in this response. An in-memory
        cache cannot honour that — persisting it is a config write — so the only safe answer is to fail immediately and
        say so.

        Failing here, on the first exchange, is the whole point. Discarding the replacement silently buys one working
        call and then an opaque ``400`` once the access token expires, naming neither the cause nor the fix. A no-op
        when the endpoint echoes the SAME credential back, which is benign and common. *setting* names where the
        operator configured the credential, for the error message.
        """
        if returned and returned != configured:
            msg = (
                f"{provider} returned a NEW long-lived credential, which means this key is configured for "
                f"ROTATING credentials. This client cannot use that: the replacement would have to be persisted "
                f"durably on every exchange, and the configured {setting} is now dead. Re-authorise with a "
                f"non-rotating key and set {setting} to the new value."
            )
            raise ClientConfigurationError(
                msg,
                source=provider,
            )

    # -- Cache ---------------------------------------------------------------

    def _usable(self, rejected: TokenT | None) -> TokenT | None:
        """Return the cached token if present, not the one a 401 just rejected, and not within the refresh margin."""
        cached = self._token
        if cached is not None and cached != rejected and time.monotonic() < self._expires_at - self._margin_s:
            return cached
        return None

    def _remember(self, token: TokenT, lifetime: float) -> TokenT:
        self._token = token
        self._minted_at = time.monotonic()
        self._expires_at = self._minted_at + lifetime
        # A lifetime at or under the refresh margin would read as due the moment it was minted, so every request
        # would mint again: refresh at half-life when the margin would swallow the lifetime.
        self._margin_s = min(self._refresh_margin_s, lifetime / 2)
        return token

    async def _access_token(self, *, rejected: TokenT | None = None) -> TokenT:
        """Return the cached token, minting one when absent, near expiry, or just rejected (by :meth:`_mint_token`).

        ``rejected`` is the token a 401 just came back for. Passing it — rather than a bare ``force`` flag — is what
        makes concurrent 401s collapse: each waiter re-checks under the lock, sees that the cached token is no longer
        the one IT was rejected for, and uses the replacement the first waiter already minted. With a flag, every waiter
        would skip the cache check and mint its own.

        The auth flow does the same under the same lock, but drives :meth:`_mint_flow` so a mint can yield requests.
        """
        async with self._lock:
            if (cached := self._usable(rejected)) is not None:
                return cached
            return self._remember(*await self._mint_token())

    def _is_fresh(self, token: TokenT) -> bool:
        """Whether *token* is still the cached one AND was minted within ``_FRESH_TOKEN_S``.

        A rejected token that is no longer the cached one has already been replaced by another request, so it is not
        fresh in the sense that matters: the replacement is worth a retry, and ``_access_token`` hands it over without
        minting.
        """
        return token == self._token and time.monotonic() - self._minted_at < _FRESH_TOKEN_S

    @override
    async def async_auth_flow(self, request: httpx2.Request) -> AsyncGenerator[httpx2.Request, httpx2.Response]:
        if self._skips(request):
            yield request
            return

        # The 401 path re-sends this request, so its body must be buffered first — a streamed body would otherwise be
        # consumed by the first send. Read here, explicitly:
        # httpx2 honours ``requires_request_body = True`` only in its DEFAULT flows, and this method overrides them.
        await request.aread()
        rejected: TokenT | None = None
        for attempt in range(2):
            async with self._lock:
                token = self._usable(rejected)
                if token is None:
                    # Drive the mint flow here, because only this generator can yield to the client: each request the
                    # mint asks for is sent by httpx2, and its response is handed back in.
                    mint = self._mint_flow(request)
                    try:
                        step = await anext(mint)
                        while not isinstance(step, Minted):
                            step = await mint.asend((yield step))
                    finally:
                        await mint.aclose()
                    token = self._remember(step.token, step.lifetime)
            self._attach(request, token)
            response = yield request
            if response.status_code != httpx2.codes.UNAUTHORIZED or attempt or self._is_fresh(token):
                return
            rejected = token
