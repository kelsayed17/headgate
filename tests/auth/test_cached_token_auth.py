# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""``CachedTokenAuth``: the shared token-cache policy, tested without a provider.

A counting fake stands in for the token exchange, so caching, the expiry margin, the single 401 re-mint and the
fresh-token rule are pinned independently of any provider's request shape. A controlled clock replaces
``time.monotonic`` wherever a test needs time to pass.
"""

from __future__ import annotations

import asyncio
import math
from typing import TYPE_CHECKING, override

import httpx2
import pytest

from headgate import _auth
from headgate._auth import AUTH_HOP, FRESH_TOKEN_S, CachedTokenAuth, Minted, failed_on_auth_hop
from headgate.errors import ClientConfigurationError
from tests.support.tokens import TEST_BEARER, premint

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator, Callable

#: The default ``refresh_margin``.
_MARGIN_S = 120.0
#: A lifetime well past the margin, so only the margin decides when the token is due.
_LIFETIME_S = 1200.0
#: Exchanges for one initial mint plus one replacement.
_MINT_AND_RE_MINT = 2
#: Long enough past a mint that a 401 counts as staleness, short enough that the token is still usable.
_PAST_THE_FRESH_WINDOW_S = FRESH_TOKEN_S + 1


class _CountingAuth(CachedTokenAuth):
    """Mint ``tok-1``, ``tok-2``, ... and count exchanges, with no network."""

    def __init__(self, *, lifetime: float = _LIFETIME_S, delay: float = 0.0) -> None:
        """Mint tokens living *lifetime* seconds, each exchange taking *delay* seconds."""
        super().__init__()
        self.mints = 0
        self._lifetime = lifetime
        self._delay = delay

    @override
    async def mint_token(self) -> tuple[str, float]:
        if self._delay:
            # Widen the window in which concurrent callers pile up, so a missing lock shows as extra exchanges.
            await asyncio.sleep(self._delay)
        self.mints += 1
        return f"tok-{self.mints}", self._lifetime


class _Clock:
    """A ``time.monotonic`` stand-in that moves only when a test advances it."""

    def __init__(self) -> None:
        """Start at an arbitrary positive instant."""
        self.now = 1000.0

    def monotonic(self) -> float:
        """Return the current instant."""
        return self.now

    def advance(self, seconds: float) -> None:
        """Move the clock forward by *seconds*."""
        self.now += seconds


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    """Replace the auth module's clock with one the test controls."""
    fake = _Clock()
    monkeypatch.setattr(_auth, "time", fake)
    return fake


type _Factory = Callable[[httpx2.Auth, Callable[[httpx2.Request], httpx2.Response]], httpx2.AsyncClient]


@pytest.fixture
async def client_factory() -> AsyncIterator[_Factory]:
    """Build httpx2 clients over a handler, so the real auth flow runs, and close them afterwards."""
    created: list[httpx2.AsyncClient] = []

    def build(auth: httpx2.Auth, handler: Callable[[httpx2.Request], httpx2.Response]) -> httpx2.AsyncClient:
        client = httpx2.AsyncClient(auth=auth, transport=httpx2.MockTransport(handler), base_url="https://example.test")
        created.append(client)
        return client

    try:
        yield build
    finally:
        for c in created:
            await c.aclose()


def _ok(request: httpx2.Request) -> httpx2.Response:
    return httpx2.Response(httpx2.codes.OK, json={"seen": request.headers.get("Authorization")})


def _unauthorized(_request: httpx2.Request) -> httpx2.Response:
    return httpx2.Response(httpx2.codes.UNAUTHORIZED)


def _get(url: str = "https://api.example/x") -> httpx2.Request:
    return httpx2.Request("GET", url)


class TestCaching:
    """One exchange serves every request until the token is due."""

    async def test_one_exchange_serves_many_requests(self, client_factory: _Factory) -> None:
        """Reuse the cached token across sequential requests."""
        auth = _CountingAuth()
        client = client_factory(auth, _ok)

        for _ in range(4):
            await client.get("/x")

        assert auth.mints == 1

    async def test_concurrent_first_requests_share_one_exchange(self, client_factory: _Factory) -> None:
        """Serialise the first mint: without the lock each in-flight request fires its own exchange."""
        auth = _CountingAuth(delay=0.01)
        client = client_factory(auth, _ok)

        await asyncio.gather(*(client.get("/x") for _ in range(8)))

        assert auth.mints == 1

    async def test_a_token_inside_the_expiry_margin_is_re_minted(self, client_factory: _Factory, clock: _Clock) -> None:
        """Re-mint before expiry: a token that dies mid-flight fails a request that may be an unrepeatable write."""
        auth = _CountingAuth()
        client = client_factory(auth, _ok)
        due_after = _LIFETIME_S - _MARGIN_S

        await client.get("/x")
        clock.advance(due_after - 1)
        await client.get("/x")
        assert auth.mints == 1, "still outside the margin"

        clock.advance(2)
        await client.get("/x")
        assert auth.mints == _MINT_AND_RE_MINT, "inside the margin and before expiry"

    async def test_a_token_is_due_exactly_at_the_margin(self, client_factory: _Factory, clock: _Clock) -> None:
        """Re-mint at the very instant the margin begins, not one tick later."""
        auth = _CountingAuth()
        client = client_factory(auth, _ok)

        await client.get("/x")
        clock.advance(_LIFETIME_S - _MARGIN_S)
        await client.get("/x")

        assert auth.mints == _MINT_AND_RE_MINT

    async def test_a_lifetime_inside_the_refresh_margin_is_reused_until_half_life(
        self, client_factory: _Factory, clock: _Clock
    ) -> None:
        """Refresh a lifetime at or below the margin at half-life, not on every request."""
        lifetime = _MARGIN_S * 0.75
        auth = _CountingAuth(lifetime=lifetime)
        client = client_factory(auth, _ok)

        for _ in range(5):
            await client.get("/x")
        assert auth.mints == 1, "reused, not re-minted per request"

        clock.advance(lifetime / 2 + 1)
        await client.get("/x")
        assert auth.mints == _MINT_AND_RE_MINT, "refreshed at half-life, still before expiry"


@pytest.fixture
def held_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    """Count every token as long-held, so a 401 on it re-mints.

    These tests are about the re-mint itself (a token revoked partway through its lifetime), so they close the
    fresh-token window rather than wait it out. ``TestAFreshTokenIsNotReMinted`` covers the window.
    """
    monkeypatch.setattr(_auth, "FRESH_TOKEN_S", 0.0)


@pytest.mark.usefixtures("held_tokens")
class TestRejection:
    """A 401 on a long-held token re-mints once, never in a loop."""

    async def test_a_401_re_mints_exactly_once(self, client_factory: _Factory) -> None:
        """Re-send the rejected request once, with the replacement token."""
        seen: list[str | None] = []

        def handler(request: httpx2.Request) -> httpx2.Response:
            seen.append(request.headers.get("Authorization"))
            return httpx2.Response(httpx2.codes.UNAUTHORIZED if len(seen) == 1 else httpx2.codes.OK)

        auth = _CountingAuth()
        response = await client_factory(auth, handler).get("/x")

        assert response.status_code == httpx2.codes.OK
        assert seen == ["Bearer tok-1", "Bearer tok-2"]

    async def test_a_persistent_401_does_not_loop(self, client_factory: _Factory) -> None:
        """Hand the second 401 to the caller after one re-mint."""
        auth = _CountingAuth()

        response = await client_factory(auth, _unauthorized).get("/x")

        assert response.status_code == httpx2.codes.UNAUTHORIZED
        assert auth.mints == _MINT_AND_RE_MINT

    async def test_concurrent_401s_collapse_to_a_single_re_mint(self, client_factory: _Factory) -> None:
        """Let every waiter reuse the first replacement, because each re-checks against the token it was rejected for.

        A bare "force a re-mint" flag would skip that check and mint once per waiter: five exchanges for four 401s.
        """

        def handler(request: httpx2.Request) -> httpx2.Response:
            rejected = request.headers.get("Authorization", "").endswith("tok-1")
            return httpx2.Response(httpx2.codes.UNAUTHORIZED if rejected else httpx2.codes.OK)

        auth = _CountingAuth(delay=0.01)

        await asyncio.gather(*(client_factory(auth, handler).get("/x") for _ in range(4)))

        assert auth.mints == _MINT_AND_RE_MINT

    async def test_a_streamed_body_survives_the_401_re_send(self) -> None:
        """Buffer a streamed body before the first send, because the 401 path re-sends the request.

        ``requires_request_body = True`` would not do it: httpx2 honours that only in its default flows, which this
        class overrides.
        """
        body = b'{"order": 1}'

        async def stream() -> AsyncIterator[bytes]:
            yield body

        flow = _CountingAuth().async_auth_flow(httpx2.Request("POST", "https://api.example/orders", content=stream()))
        first = await anext(flow)
        second = await flow.asend(httpx2.Response(httpx2.codes.UNAUTHORIZED, request=first))

        assert second.content == body
        assert second.headers["Authorization"] == "Bearer tok-2"


class TestAFreshTokenIsNotReMinted:
    """A 401 on a token minted within ``FRESH_TOKEN_S`` reaches the caller without a re-mint.

    A token that new is not stale, so a replacement carries the same authority and is rejected the same way. Skipping
    the re-mint bounds a gateway that rejects valid tokens to one mint per window instead of one per request.
    """

    async def test_a_gateway_rejecting_fresh_tokens_costs_one_mint_not_one_per_request(
        self, client_factory: _Factory
    ) -> None:
        """Mint once for three rejected requests inside the window."""
        auth = _CountingAuth()
        client = client_factory(auth, _unauthorized)

        responses = [await client.get("/x") for _ in range(3)]

        assert {r.status_code for r in responses} == {httpx2.codes.UNAUTHORIZED}
        assert auth.mints == 1

    @pytest.mark.usefixtures("clock")
    async def test_a_token_held_past_the_window_is_re_minted(self, clock: _Clock) -> None:
        """Re-mint on a 401 once the token has outlived the window."""
        auth = _CountingAuth()
        flow = auth.async_auth_flow(_get())
        first = await anext(flow)
        clock.advance(_PAST_THE_FRESH_WINDOW_S)

        second = await flow.asend(httpx2.Response(httpx2.codes.UNAUTHORIZED, request=first))

        assert second.headers["Authorization"] == "Bearer tok-2"
        assert auth.mints == _MINT_AND_RE_MINT

    async def test_a_token_exactly_as_old_as_the_window_is_re_minted(self, clock: _Clock) -> None:
        """Count a token minted exactly ``FRESH_TOKEN_S`` ago as stale: the window is open at its end."""
        auth = _CountingAuth()
        flow = auth.async_auth_flow(_get())
        first = await anext(flow)
        clock.advance(FRESH_TOKEN_S)

        second = await flow.asend(httpx2.Response(httpx2.codes.UNAUTHORIZED, request=first))

        assert second.headers["Authorization"] == "Bearer tok-2"

    async def test_a_late_401_for_an_already_replaced_token_retries_with_the_replacement(self, clock: _Clock) -> None:
        """Judge freshness on the token rejected, not on the cache: a replaced one is worth a mint-free retry."""
        auth = _CountingAuth()
        a = auth.async_auth_flow(_get("https://api.example/a"))
        b = auth.async_auth_flow(_get("https://api.example/b"))
        sent_a = await anext(a)
        sent_b = await anext(b)  # both on tok-1
        clock.advance(_PAST_THE_FRESH_WINDOW_S)

        retried_a = await a.asend(httpx2.Response(httpx2.codes.UNAUTHORIZED, request=sent_a))  # mints tok-2, now fresh
        retried_b = await b.asend(httpx2.Response(httpx2.codes.UNAUTHORIZED, request=sent_b))  # rejected the old tok-1

        assert retried_a.headers["Authorization"] == retried_b.headers["Authorization"] == "Bearer tok-2"
        assert auth.mints == _MINT_AND_RE_MINT


class TestSeed:
    """``seed`` caches a token obtained elsewhere."""

    async def test_a_seeded_token_is_used_without_minting(self, client_factory: _Factory) -> None:
        """Stamp the seeded token, and never exchange for one."""
        auth = premint(_CountingAuth())

        response = await client_factory(auth, _ok).get("/x")

        assert response.json()["seen"] == f"Bearer {TEST_BEARER}"
        assert auth.mints == 0

    async def test_a_seeded_token_counts_as_long_held(self) -> None:
        """Re-mint on a 401, because a seeded token's age is unknown and so it is never fresh."""
        auth = premint(_CountingAuth())
        flow = auth.async_auth_flow(_get())
        first = await anext(flow)

        second = await flow.asend(httpx2.Response(httpx2.codes.UNAUTHORIZED, request=first))

        assert second.headers["Authorization"] == "Bearer tok-1"
        assert auth.mints == 1

    async def test_a_seeded_lifetime_is_counted_from_the_seed(self, client_factory: _Factory, clock: _Clock) -> None:
        """Re-mint once the seeded token's remaining lifetime enters the margin."""
        auth = _CountingAuth()
        auth.seed("restored", _LIFETIME_S)
        client = client_factory(auth, _ok)

        clock.advance(_LIFETIME_S - _MARGIN_S - 1)
        assert (await client.get("/x")).json()["seen"] == "Bearer restored"
        clock.advance(2)
        assert (await client.get("/x")).json()["seen"] == "Bearer tok-1"


class TestPrecedence:
    """A caller's own credential wins."""

    async def test_an_explicit_authorization_header_wins(self, client_factory: _Factory) -> None:
        """Skip the flow entirely for a caller's ``Authorization``: no mint, and the caller's header is sent."""
        auth = _CountingAuth()

        response = await client_factory(auth, _ok).get("/x", headers={"Authorization": "Bearer caller"})

        assert auth.mints == 0
        assert response.json()["seen"] == "Bearer caller"


class TestRotationRefusal:
    """``reject_credential_rotation`` fails fast on a rotated long-lived credential."""

    def test_a_changed_credential_is_refused_by_name(self) -> None:
        """Name the rotation and the setting to fix in the error."""
        with pytest.raises(ClientConfigurationError, match="rotating") as exc:
            _CountingAuth().reject_credential_rotation("new-token", "configured-token", setting="EXAMPLE_REFRESH_TOKEN")

        assert "EXAMPLE_REFRESH_TOKEN" in str(exc.value)
        assert exc.value.source == "_CountingAuth"
        assert str(exc.value).startswith("[_CountingAuth] _CountingAuth's token endpoint returned a new long-lived")
        assert "new-token" not in str(exc.value)

    @pytest.mark.parametrize("returned", [None, "", "same"], ids=["absent", "empty", "echoed"])
    def test_an_absent_or_echoed_credential_is_benign(self, returned: str | None) -> None:
        """Accept a missing, empty or echoed credential: only a different one means rotation."""
        _CountingAuth().reject_credential_rotation(returned, "same", setting="EXAMPLE_REFRESH_TOKEN")


class _HoppingAuth(CachedTokenAuth[tuple[str, str]]):
    """Mint through requests the flow yields (a cookie-plus-crumb exchange) and ride as a header pair."""

    token_headers = frozenset({"X-Cookie", "X-Crumb"})

    def __init__(self) -> None:
        """Start with no mints counted."""
        super().__init__()
        self.mints = 0

    @override
    async def mint_flow(
        self, request: httpx2.Request
    ) -> AsyncGenerator[httpx2.Request | Minted[tuple[str, str]], httpx2.Response]:
        self.mints += 1
        seed = yield httpx2.Request("GET", "https://seed.test/seed")
        second = yield httpx2.Request("GET", "https://seed.test/token", headers={"X-Seed": seed.headers["x-cookie"]})
        yield Minted((seed.headers["x-cookie"], second.text), math.inf)

    @override
    def attach(self, request: httpx2.Request, token: tuple[str, str]) -> None:
        request.headers["X-Cookie"], request.headers["X-Crumb"] = token

    @override
    def skips(self, request: httpx2.Request) -> bool:
        return request.url.path.startswith("/open")


def _crumb_server(seen: list[str]) -> Callable[[httpx2.Request], httpx2.Response]:
    """Serve the cookie-plus-crumb exchange and echo the pair a data request carries, recording every request."""

    def handle(request: httpx2.Request) -> httpx2.Response:
        seen.append(f"{request.url.host}{request.url.path}")
        if request.url.path == "/seed":
            return httpx2.Response(httpx2.codes.OK, headers={"x-cookie": "c1"})
        if request.url.path == "/token":
            return httpx2.Response(httpx2.codes.OK, text="crumb-1")
        return httpx2.Response(
            httpx2.codes.OK, json={"cookie": request.headers.get("x-cookie"), "crumb": request.headers.get("x-crumb")}
        )

    return handle


class TestAMintThatYieldsRequests:
    """``mint_flow``: the exchange's requests are sent by the client's own stack, under the same cache and lock."""

    async def test_the_hops_go_through_the_client_and_the_pair_is_attached(self, client_factory: _Factory) -> None:
        """Send each hop through the client, then attach the minted pair to the call."""
        seen: list[str] = []

        resp = await client_factory(_HoppingAuth(), _crumb_server(seen)).get("/data")

        assert resp.json() == {"cookie": "c1", "crumb": "crumb-1"}
        assert seen == ["seed.test/seed", "seed.test/token", "example.test/data"]

    async def test_every_hop_is_marked_as_an_auth_hop(self, client_factory: _Factory) -> None:
        """Mark each yielded request with ``AUTH_HOP``, whether or not the subclass did, and not the call itself."""
        marks: dict[str, bool] = {}
        serve = _crumb_server([])

        def handle(request: httpx2.Request) -> httpx2.Response:
            marks[request.url.path] = bool(request.extensions.get(AUTH_HOP))
            return serve(request)

        await client_factory(_HoppingAuth(), handle).get("/data")

        assert marks == {"/seed": True, "/token": True, "/data": False}

    async def test_a_failed_hop_is_recognised_as_an_auth_hop_failure(self, client_factory: _Factory) -> None:
        """Let the breaker see a hop's failure as the credential host's, even for a hop the subclass left unmarked."""

        def handle(request: httpx2.Request) -> httpx2.Response:
            msg = "down"
            raise httpx2.ConnectError(msg, request=request)

        with pytest.raises(httpx2.ConnectError) as exc:
            await client_factory(_HoppingAuth(), handle).get("/data")

        assert failed_on_auth_hop(exc.value)

    async def test_the_pair_is_cached_and_concurrent_first_calls_share_one_mint(self, client_factory: _Factory) -> None:
        """Hold the lock across the hops, so concurrent first calls wait for one mint and reuse it."""
        seen: list[str] = []
        auth = _HoppingAuth()
        client = client_factory(auth, _crumb_server(seen))

        await asyncio.gather(*(client.get("/data") for _ in range(4)))
        await client.get("/data")

        assert auth.mints == 1
        assert seen.count("seed.test/seed") == 1

    async def test_a_skipped_request_neither_mints_nor_carries_the_pair(self, client_factory: _Factory) -> None:
        """Send a request the auth skips as it is, without minting."""
        seen: list[str] = []
        auth = _HoppingAuth()

        resp = await client_factory(auth, _crumb_server(seen)).get("/open/x")

        assert resp.json() == {"cookie": None, "crumb": None}
        assert auth.mints == 0
        assert seen == ["example.test/open/x"]

    async def test_a_failure_while_minting_leaves_no_token_and_the_next_request_mints_again(
        self, client_factory: _Factory
    ) -> None:
        """Release the lock and cache nothing when a hop fails, so the next request starts a fresh mint."""
        auth = _HoppingAuth()
        failing = True
        serve = _crumb_server([])

        def handle(request: httpx2.Request) -> httpx2.Response:
            if request.url.path == "/seed" and failing:
                msg = "down"
                raise httpx2.ConnectError(msg, request=request)
            return serve(request)

        client = client_factory(auth, handle)
        with pytest.raises(httpx2.ConnectError):
            await client.get("/data")
        failing = False
        resp = await client.get("/data")

        assert resp.json() == {"cookie": "c1", "crumb": "crumb-1"}
        assert auth.mints == _MINT_AND_RE_MINT

    async def test_cancelling_a_call_mid_hop_releases_the_lock(self) -> None:
        """Release the lock when the flow is closed while a hop is in flight, so the next request can mint."""
        auth = _HoppingAuth()
        abandoned = auth.async_auth_flow(_get())
        await anext(abandoned)  # the seed hop, sent with the lock held

        await abandoned.aclose()  # what httpx2 does when the hop raises or the caller is cancelled
        retried = auth.async_auth_flow(_get())

        assert (await anext(retried)).url.path == "/seed"

    async def test_a_flow_that_never_yields_minted_is_a_clear_error(self) -> None:
        """Name the subclass when its mint flow ends without a credential."""

        class _Unfinished(CachedTokenAuth):
            """Mint through a flow that yields nothing."""

            @override
            async def mint_flow(
                self, request: httpx2.Request
            ) -> AsyncGenerator[httpx2.Request | Minted[str], httpx2.Response]:
                if request.url.path == "/never":
                    yield Minted("unreachable", 0.0)

        with pytest.raises(RuntimeError, match=r"_Unfinished\.mint_flow ended without yielding Minted"):
            await anext(_Unfinished().async_auth_flow(_get()))


class TestASubclassMustSupplyAMint:
    """A subclass supplies ``mint_token`` or ``mint_flow``, checked when the class is defined."""

    def test_neither_hook_overridden_fails_at_the_class_statement(self) -> None:
        """Refuse a class that overrides neither."""
        with pytest.raises(TypeError, match="must override mint_token or mint_flow"):

            class _Neither(CachedTokenAuth):
                """Supply no mint."""

    async def test_an_override_inherited_from_a_parent_counts(self) -> None:
        """Accept a subclass of a provider's auth, which supplies no mint of its own."""

        class _Child(_CountingAuth):
            """Inherit the parent's mint."""

        sent = await anext(_Child().async_auth_flow(_get()))

        assert sent.headers["Authorization"] == "Bearer tok-1"

    async def test_a_mint_flow_alone_is_enough_and_the_flow_wins_when_both_exist(self) -> None:
        """Accept a flow-only class, and run the flow rather than ``mint_token`` when a class has both."""

        class _FlowOnly(CachedTokenAuth):
            """Mint through a flow only."""

            @override
            async def mint_flow(
                self, request: httpx2.Request
            ) -> AsyncGenerator[httpx2.Request | Minted[str], httpx2.Response]:
                yield Minted("from-flow", math.inf)

        class _Both(_CountingAuth):
            """Mint through both hooks."""

            @override
            async def mint_flow(
                self, request: httpx2.Request
            ) -> AsyncGenerator[httpx2.Request | Minted[str], httpx2.Response]:
                yield Minted("from-flow", math.inf)

        flow_only = await anext(_FlowOnly().async_auth_flow(_get()))
        both = _Both()
        sent = await anext(both.async_auth_flow(_get()))

        assert flow_only.headers["Authorization"] == sent.headers["Authorization"] == "Bearer from-flow"
        assert both.mints == 0

    async def test_the_default_mint_token_names_the_flow_only_class(self) -> None:
        """Raise a clear error when a flow-only class falls back to the default ``mint_token``."""

        class _Delegating(CachedTokenAuth):
            """Override the flow, then delegate to the default one, which calls ``mint_token``."""

            @override
            async def mint_flow(
                self, request: httpx2.Request
            ) -> AsyncGenerator[httpx2.Request | Minted[str], httpx2.Response]:
                async for step in super().mint_flow(request):
                    yield step

        with pytest.raises(NotImplementedError, match="_Delegating mints through mint_flow"):
            await anext(_Delegating().async_auth_flow(_get()))


class TestFailedOnAuthHop:
    """``failed_on_auth_hop`` recognises only a request error stamped with a marked request."""

    def test_an_error_on_a_marked_request_is_an_auth_hop_failure(self) -> None:
        """Recognise a request error whose request carries the marker."""
        hop = httpx2.Request("GET", "https://seed.test/", extensions={AUTH_HOP: True})

        assert failed_on_auth_hop(httpx2.ConnectError("down", request=hop))

    def test_an_error_on_the_calls_own_request_is_not(self) -> None:
        """Charge an unmarked request's failure to its host."""
        assert not failed_on_auth_hop(httpx2.ConnectError("down", request=_get()))

    def test_an_error_built_without_a_request_is_not(self) -> None:
        """Treat a request error with no request attached, as a transport double may raise, as the call's own."""
        assert not failed_on_auth_hop(httpx2.ConnectError("down"))

    def test_an_error_that_is_not_a_request_error_is_not(self) -> None:
        """Ignore exceptions httpx2 does not stamp with a request, such as a bare ``OSError``."""
        assert not failed_on_auth_hop(OSError("reset"))
