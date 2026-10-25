"""``CachedTokenAuth``: the shared token-cache policy, tested without a provider.

A counting fake stands in for the token exchange, so caching, the expiry margin, the single 401 re-mint and the
fresh-token rule are pinned independently of any provider's request shape.
"""

from __future__ import annotations

import asyncio
import math
from types import SimpleNamespace
from typing import TYPE_CHECKING, override

import httpx2
import pytest

from headgate import _auth
from headgate._auth import CachedTokenAuth, Minted
from headgate.errors import ClientConfigurationError
from tests.support.tokens import premint

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator, Callable


class _CountingAuth(CachedTokenAuth):
    """Mints ``tok-1``, ``tok-2``, … and counts exchanges. No network."""

    def __init__(self, *, lifetime: float = 1200.0, delay: float = 0.0) -> None:
        super().__init__()
        self.mints = 0
        self._lifetime = lifetime
        self._delay = delay

    @override
    async def _mint_token(self) -> tuple[str, float]:
        if self._delay:
            # Widen the window in which concurrent callers can pile up, so a
            # missing lock would actually show as extra exchanges.
            await asyncio.sleep(self._delay)
        self.mints += 1
        return f"tok-{self.mints}", self._lifetime


type _Factory = Callable[[httpx2.Auth, Callable[[httpx2.Request], httpx2.Response]], httpx2.AsyncClient]


@pytest.fixture
async def client_factory() -> AsyncIterator[_Factory]:
    """Builds an httpx2 client over a handler, so the real auth flow runs."""
    created: list[httpx2.AsyncClient] = []

    def _build(auth: httpx2.Auth, handler: Callable[[httpx2.Request], httpx2.Response]) -> httpx2.AsyncClient:
        client = httpx2.AsyncClient(
            auth=auth,
            transport=httpx2.MockTransport(handler),
            base_url="https://example.test",
        )
        created.append(client)
        return client

    try:
        yield _build
    finally:
        for c in created:
            await c.aclose()


def _ok(request: httpx2.Request) -> httpx2.Response:
    return httpx2.Response(200, json={"seen": request.headers.get("Authorization")})


class TestCaching:
    async def test_one_exchange_serves_many_requests(self, client_factory: _Factory) -> None:
        auth = _CountingAuth()
        client = client_factory(auth, _ok)

        for _ in range(4):
            await client.get("/x")

        assert auth.mints == 1

    async def test_concurrent_first_requests_share_one_exchange(self, client_factory: _Factory) -> None:
        """The lock: without it each in-flight request fires its own exchange."""
        auth = _CountingAuth(delay=0.01)
        client = client_factory(auth, _ok)

        await asyncio.gather(*(client.get("/x") for _ in range(8)))

        assert auth.mints == 1

    async def test_a_token_inside_the_expiry_margin_is_re_minted(
        self, client_factory: _Factory, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Re-mint BEFORE expiry: a token that dies mid-flight fails a request that may be an unrepeatable write.

        Driven by a controlled clock with a lifetime well past the margin; a lifetime inside the margin is
        ``TestShortLifetime``'s case.
        """
        clock = [1000.0]
        monkeypatch.setattr(_auth, "time", SimpleNamespace(monotonic=lambda: clock[0]))
        auth = _CountingAuth(lifetime=1200.0)  # default 120 s margin: refresh from 1080 s after the mint
        client = client_factory(auth, _ok)

        await client.get("/x")
        clock[0] += 1079.0
        await client.get("/x")
        assert auth.mints == 1, "still outside the margin"

        clock[0] += 2.0
        await client.get("/x")
        assert auth.mints == 2, "inside the margin and before expiry"


@pytest.fixture
def held_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every token counts as long-held, so a 401 on it re-mints.

    A token rejected within ``_FRESH_TOKEN_S`` of its mint is NOT re-minted. These tests are about the
    re-mint itself — a token revoked partway through its lifetime — so they zero the window rather than wait it out.
    ``TestAFreshTokenIsNotReMinted`` covers the window.
    """
    monkeypatch.setattr(_auth, "_FRESH_TOKEN_S", 0.0)


@pytest.mark.usefixtures("held_tokens")
class TestRejection:
    async def test_a_401_re_mints_exactly_once(self, client_factory: _Factory) -> None:
        seen: list[str | None] = []

        def handler(request: httpx2.Request) -> httpx2.Response:
            seen.append(request.headers.get("Authorization"))
            return httpx2.Response(401 if len(seen) == 1 else 200)

        auth = _CountingAuth()
        client = client_factory(auth, handler)

        response = await client.get("/x")

        assert response.status_code == 200
        assert auth.mints == 2
        assert len(seen) == 2

    async def test_a_persistent_401_does_not_loop(self, client_factory: _Factory) -> None:
        auth = _CountingAuth()
        client = client_factory(auth, lambda _r: httpx2.Response(401))

        response = await client.get("/x")

        assert response.status_code == 401
        # One re-mint, then give up — never an auth loop.
        assert auth.mints == 2

    async def test_concurrent_401s_collapse_to_a_single_re_mint(self, client_factory: _Factory) -> None:
        """Each waiter re-checks the cache against the token IT was rejected for, so all reuse the first replacement.

        A bare "force a re-mint" flag would skip that check and mint once per waiter: five exchanges for four 401s.
        """

        def handler(request: httpx2.Request) -> httpx2.Response:
            # The first token is always rejected; any replacement is accepted.
            return httpx2.Response(401 if request.headers.get("Authorization", "").endswith("tok-1") else 200)

        auth = _CountingAuth(delay=0.01)
        client = client_factory(auth, handler)

        await asyncio.gather(*(client.get("/x") for _ in range(4)))

        # One initial mint plus exactly one replacement, not one per waiter.
        assert auth.mints == 2


class TestPrecedence:
    async def test_an_explicit_authorization_header_wins(self, client_factory: _Factory) -> None:
        """A caller's ``Authorization`` skips the flow entirely: no mint, and the caller's header is sent."""
        auth = _CountingAuth()
        client = client_factory(auth, _ok)

        response = await client.get("/x", headers={"Authorization": "Bearer caller"})

        assert auth.mints == 0
        assert response.json()["seen"] == "Bearer caller"


class TestRotationRefusal:
    def test_a_changed_credential_is_refused_by_name(self) -> None:
        with pytest.raises(ClientConfigurationError) as exc:
            CachedTokenAuth.reject_credential_rotation(
                "new-token", "configured-token", provider="Example", setting="EXAMPLE_REFRESH_TOKEN"
            )

        message = str(exc.value)
        assert "ROTATING" in message
        assert "EXAMPLE_REFRESH_TOKEN" in message

    @pytest.mark.parametrize("returned", [None, "", "same"], ids=["absent", "empty", "echoed"])
    def test_an_absent_or_echoed_credential_is_benign(self, returned: str | None) -> None:
        """Only a DIFFERENT credential means rotation; echoing the same one back is common."""
        CachedTokenAuth.reject_credential_rotation(
            returned, "same", provider="Example", setting="EXAMPLE_REFRESH_TOKEN"
        )


@pytest.mark.usefixtures("held_tokens")
class TestTheRequestBody:
    async def test_a_streamed_body_survives_the_401_re_send(self) -> None:
        """The 401 path re-sends the request, so the flow buffers a streamed body before the first send.

        ``requires_request_body = True`` would not do it: httpx2 honours that only in its default flows, which this
        class overrides.
        """
        body = b'{"order": 1}'

        async def stream() -> AsyncIterator[bytes]:
            yield body

        flow = _CountingAuth().async_auth_flow(httpx2.Request("POST", "https://api.example/orders", content=stream()))
        first = await flow.__anext__()
        second = await flow.asend(httpx2.Response(401, request=first))

        assert second.content == body
        assert second.headers["Authorization"] == "Bearer tok-2"


class TestAFreshTokenIsNotReMinted:
    """A 401 on a token minted within ``_FRESH_TOKEN_S`` surfaces; it does not re-mint.

    A token that new is not stale, so a replacement carries the same authority and is rejected the same way. Skipping
    the re-mint bounds a gateway that rejects valid tokens to one mint per window instead of one per request.
    """

    async def test_a_gateway_rejecting_fresh_tokens_costs_one_mint_not_one_per_request(
        self, client_factory: _Factory
    ) -> None:
        auth = _CountingAuth()
        client = client_factory(auth, lambda _r: httpx2.Response(401))

        responses = [await client.get("/x") for _ in range(3)]

        assert [r.status_code for r in responses] == [401, 401, 401]
        assert auth.mints == 1

    async def test_a_token_held_past_the_window_is_re_minted(self) -> None:
        auth = _CountingAuth()
        flow = auth.async_auth_flow(httpx2.Request("GET", "https://api.example/x"))
        first = await flow.__anext__()
        auth._minted_at -= _auth._FRESH_TOKEN_S + 1  # held for longer than the window

        second = await flow.asend(httpx2.Response(401, request=first))

        assert second.headers["Authorization"] == "Bearer tok-2"
        assert auth.mints == 2

    async def test_a_late_401_for_an_already_replaced_token_retries_with_the_replacement(self) -> None:
        """Freshness is judged on the token REJECTED, not on the cache: a replaced one is worth a retry, mint-free."""
        auth = _CountingAuth()
        a = auth.async_auth_flow(httpx2.Request("GET", "https://api.example/a"))
        b = auth.async_auth_flow(httpx2.Request("GET", "https://api.example/b"))
        sent_a = await a.__anext__()
        sent_b = await b.__anext__()  # both on tok-1
        auth._minted_at -= _auth._FRESH_TOKEN_S + 1

        retried_a = await a.asend(httpx2.Response(401, request=sent_a))  # re-mints tok-2, which is now fresh
        retried_b = await b.asend(httpx2.Response(401, request=sent_b))  # rejected tok-1, not the fresh tok-2

        assert retried_a.headers["Authorization"] == retried_b.headers["Authorization"] == "Bearer tok-2"
        assert auth.mints == 2

    async def test_a_preminted_token_counts_as_long_held(self) -> None:
        """``premint`` stands in for a token held for minutes, so a 401 on it re-mints, as endpoint tests expect."""
        auth = premint(_CountingAuth())
        flow = auth.async_auth_flow(httpx2.Request("GET", "https://api.example/x"))
        first = await flow.__anext__()

        second = await flow.asend(httpx2.Response(401, request=first))

        assert second.headers["Authorization"] == "Bearer tok-1"
        assert auth.mints == 1


class TestShortLifetime:
    async def test_a_lifetime_inside_the_refresh_margin_is_reused_until_half_life(
        self, client_factory: _Factory, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A lifetime at or below the margin (120 s by default) refreshes at half-life, not on every request."""
        clock = [1000.0]
        monkeypatch.setattr(_auth, "time", SimpleNamespace(monotonic=lambda: clock[0]))
        auth = _CountingAuth(lifetime=90.0)
        client = client_factory(auth, _ok)

        for _ in range(5):
            await client.get("/x")
        assert auth.mints == 1, "reused, not re-minted per request"

        clock[0] += 46.0
        await client.get("/x")
        assert auth.mints == 2, "refreshed at half-life, still before the 90 s expiry"


class _HoppingAuth(CachedTokenAuth[tuple[str, str]]):
    """Mints through requests the flow yields (a cookie-plus-crumb exchange) and rides as a header pair."""

    def __init__(self) -> None:
        super().__init__()
        self.mints = 0

    @override
    async def _mint_flow(
        self, request: httpx2.Request
    ) -> AsyncGenerator[httpx2.Request | Minted[tuple[str, str]], httpx2.Response]:
        self.mints += 1
        seed = yield httpx2.Request("GET", "https://seed.test/seed", extensions={_auth.AUTH_HOP: True})
        second = yield httpx2.Request("GET", "https://seed.test/token", headers={"X-Seed": seed.headers["x-cookie"]})
        yield Minted((seed.headers["x-cookie"], second.text), math.inf)

    @override
    def _attach(self, request: httpx2.Request, token: tuple[str, str]) -> None:
        request.headers["X-Cookie"], request.headers["X-Crumb"] = token

    @override
    def _skips(self, request: httpx2.Request) -> bool:
        return request.url.path.startswith("/open")


class TestAMintThatYieldsRequests:
    """``_mint_flow``: the exchange's requests are sent by the client's own stack, under the same cache and lock."""

    @staticmethod
    def _handler(seen: list[str]) -> Callable[[httpx2.Request], httpx2.Response]:
        def handle(request: httpx2.Request) -> httpx2.Response:
            seen.append(f"{request.url.host}{request.url.path}")
            if request.url.path == "/seed":
                return httpx2.Response(200, headers={"x-cookie": "c1"})
            if request.url.path == "/token":
                return httpx2.Response(200, text="crumb-1")
            return httpx2.Response(
                200, json={"cookie": request.headers.get("x-cookie"), "crumb": request.headers.get("x-crumb")}
            )

        return handle

    async def test_the_hops_go_through_the_client_and_the_pair_is_attached(self, client_factory: _Factory) -> None:
        seen: list[str] = []
        auth = _HoppingAuth()
        resp = await client_factory(auth, self._handler(seen)).get("/data")

        assert resp.json() == {"cookie": "c1", "crumb": "crumb-1"}
        assert seen == ["seed.test/seed", "seed.test/token", "example.test/data"]

    async def test_the_pair_is_cached_and_concurrent_first_calls_share_one_mint(self, client_factory: _Factory) -> None:
        seen: list[str] = []
        auth = _HoppingAuth()
        client = client_factory(auth, self._handler(seen))

        await asyncio.gather(*(client.get("/data") for _ in range(4)))
        await client.get("/data")

        assert auth.mints == 1
        assert seen.count("seed.test/seed") == 1

    async def test_a_skipped_request_neither_mints_nor_carries_the_pair(self, client_factory: _Factory) -> None:
        seen: list[str] = []
        auth = _HoppingAuth()
        resp = await client_factory(auth, self._handler(seen)).get("/open/x")

        assert resp.json() == {"cookie": None, "crumb": None}
        assert auth.mints == 0
        assert seen == ["example.test/open/x"]

    async def test_a_failure_while_minting_leaves_no_token_and_the_next_request_mints_again(
        self, client_factory: _Factory
    ) -> None:
        seen: list[str] = []
        auth = _HoppingAuth()
        failing = {"on": True}

        def handle(request: httpx2.Request) -> httpx2.Response:
            if request.url.path == "/seed" and failing["on"]:
                raise httpx2.ConnectError("down", request=request)
            return self._handler(seen)(request)

        client = client_factory(auth, handle)
        with pytest.raises(httpx2.ConnectError):
            await client.get("/data")
        failing["on"] = False
        resp = await client.get("/data")

        assert resp.json() == {"cookie": "c1", "crumb": "crumb-1"}
        assert auth.mints == 2


class TestASubclassMustSupplyAMint:
    def test_neither_hook_overridden_fails_at_the_class_statement(self) -> None:
        with pytest.raises(TypeError, match="must override _mint_token or _mint_flow"):

            class _Neither(CachedTokenAuth):
                pass

    def test_an_override_inherited_from_a_parent_counts(self) -> None:
        """A subclass of a provider's auth supplies no mint of its own and must not be refused for it."""

        class _Child(_CountingAuth):
            pass

        assert _Child()._token is None

    async def test_a_mint_flow_alone_is_enough_and_the_flow_wins_when_both_exist(self) -> None:
        class _FlowOnly(CachedTokenAuth):
            @override
            async def _mint_flow(
                self, request: httpx2.Request
            ) -> AsyncGenerator[httpx2.Request | Minted[str], httpx2.Response]:
                yield Minted("from-flow", 60.0)

        class _Both(_CountingAuth):
            @override
            async def _mint_flow(
                self, request: httpx2.Request
            ) -> AsyncGenerator[httpx2.Request | Minted[str], httpx2.Response]:
                yield Minted("from-flow", 60.0)

        assert _FlowOnly()._token is None
        both = _Both()
        sent = await anext(both.async_auth_flow(httpx2.Request("GET", "https://api.example/x")))
        assert sent.headers["Authorization"] == "Bearer from-flow"
        assert both.mints == 0
