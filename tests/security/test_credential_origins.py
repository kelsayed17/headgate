# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""Credentials reach only ``base_url``'s origin and ``trusted_origins``, on every hop; the requests still go out.

Each auth headgate ships declares what it stamps, and the credential guard withholds exactly that from any other
origin: on the first request (an absolute URL), on a redirect, and on a redirect an auth flow's own hop is answered
with. ``Authorization``, ``Proxy-Authorization`` and (without a cookie jar) ``Cookie`` are withheld whatever the auth.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar, override

import httpx2
import pytest

from headgate import CachedTokenAuth, HttpOptions, Minted, QueryParamAuth, StaticHeaderAuth
from headgate._core import CredentialStamper
from headgate.security import CHAIN_ORIGIN, CredentialGuard
from tests.security._web import BASE, Web
from tests.support.responses import RESPONSE
from tests.support.tokens import premint

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from headgate._middleware import Next


class _Bearer(CachedTokenAuth):
    """A bearer auth whose exchange never runs: tests seed it."""

    @override
    async def _mint_token(self) -> tuple[str, float]:
        raise NotImplementedError


class _HeaderToken(CachedTokenAuth):
    """A token riding its own header, declared in ``token_headers``."""

    token_headers: ClassVar = frozenset({"X-Token"})

    @override
    async def _mint_token(self) -> tuple[str, float]:
        raise NotImplementedError

    @override
    def _attach(self, request: httpx2.Request, token: str) -> None:
        request.headers["X-Token"] = token


def _auths() -> list[httpx2.Auth]:
    """Return one of each auth, ready to stamp without a network exchange."""
    return [
        StaticHeaderAuth({"X-Api-Key": "k"}),
        QueryParamAuth({"api_key": "k"}),
        premint(_Bearer()),
        premint(_HeaderToken()),
    ]


_AUTH_IDS = ["static-header", "query-param", "bearer", "custom-header-token"]


def _carries(request: httpx2.Request, auth: httpx2.Auth) -> set[str]:
    """Return which of the credentials *auth* declares *request* carries."""
    assert isinstance(auth, CredentialStamper)
    headers = {name for name in auth.credential_headers if name in request.headers}
    return headers | {name for name in auth.credential_params if name in request.url.params}


#: A key the server echoes into a redirect's ``Location``, as an open redirect that copies the query does.
_ECHOED_KEY = "?api_key=k"


@pytest.mark.parametrize("auth", _auths(), ids=_AUTH_IDS)
@pytest.mark.parametrize(
    ("base_url", "target"),
    [
        (BASE, "https://files.example/signed"),
        (BASE, "https://api.example:8443/x"),
        ("http://api.example", "https://api.example/x"),
    ],
    ids=["other-host", "other-port", "other-scheme"],
)
async def test_a_redirect_off_the_origin_is_followed_without_the_credential(
    auth: httpx2.Auth, base_url: str, target: str
) -> None:
    """Withhold every stamped credential from a hop to another host, port or scheme, and still follow it.

    httpx2 keeps ``Authorization`` on an upgrade from ``http`` to ``https`` on the same host; the guard does not, since
    the upgraded origin is not the client's.
    """
    web = Web({f"{base_url}/start": target + _ECHOED_KEY})
    async with web.client(auth=auth, base_url=base_url) as client:
        resp = await client.get("start", expect=RESPONSE)

    first, hop = web.sent
    assert resp.text == "ok"
    assert _carries(first, auth)
    assert _carries(hop, auth) == set()


@pytest.mark.parametrize("auth", _auths(), ids=_AUTH_IDS)
async def test_a_same_origin_redirect_keeps_the_credential(auth: httpx2.Auth) -> None:
    """Keep the credential on a hop that stays on the client's origin, an explicit default port included."""
    web = Web({f"{BASE}/start": "https://api.example:443/other" + _ECHOED_KEY})
    async with web.client(auth=auth) as client:
        await client.get("start", expect=RESPONSE)

    assert all(_carries(request, auth) for request in web.sent)


@pytest.mark.parametrize("auth", _auths(), ids=_AUTH_IDS)
async def test_a_trusted_origin_keeps_the_credential(auth: httpx2.Auth) -> None:
    """Send the credential to an origin listed in ``trusted_origins``, given in any normalisable form."""
    web = Web()
    trusted = HttpOptions(trusted_origins=frozenset({"HTTPS://FILES.example:443"}))
    async with web.client(auth=auth, http=trusted) as client:
        await client.get("https://files.example/signed", expect=RESPONSE)

    assert _carries(web.sent[0], auth)


async def test_a_trusted_origin_keeps_a_redirected_credential() -> None:
    """Keep a stamped header on a redirect to a trusted origin (httpx2 drops ``Authorization`` there by itself)."""
    web = Web({f"{BASE}/start": "https://files.example/signed"})
    trusted = HttpOptions(trusted_origins=frozenset({"https://files.example"}))
    async with web.client(auth=StaticHeaderAuth({"X-Api-Key": "k"}), http=trusted) as client:
        await client.get("start", expect=RESPONSE)

    assert web.sent[1].headers["X-Api-Key"] == "k"


@pytest.mark.parametrize("auth", _auths(), ids=_AUTH_IDS)
async def test_an_absolute_foreign_url_is_sent_unsigned(auth: httpx2.Auth) -> None:
    """Send a call to an absolute URL on another origin (a cursor URL) without the credential."""
    web = Web()
    async with web.client(auth=auth) as client:
        resp = await client.get("https://cdn.example/page?cursor=2", expect=RESPONSE)

    (sent,) = web.sent
    assert resp.text == "ok"
    assert _carries(sent, auth) == set()
    assert sent.url.params["cursor"] == "2"


async def test_only_the_stamped_query_parameter_is_removed() -> None:
    """Keep the call's own parameters when the auth's is removed from a foreign URL."""
    web = Web()
    async with web.client(auth=QueryParamAuth({"api_key": "k"})) as client:
        await client.get("https://cdn.example/page", expect=RESPONSE, params={"series": "Y"})

    assert dict(web.sent[0].url.params) == {"series": "Y"}


@pytest.mark.parametrize("name", ["Authorization", "Proxy-Authorization", "Cookie"])
async def test_a_credential_header_of_any_origin_is_withheld_whatever_the_auth(name: str) -> None:
    """Withhold the well-known credential headers from a foreign first request, a third-party auth's included."""
    web = Web()
    async with web.client(auth=httpx2.BasicAuth("user", "pass")) as client:
        await client.get("https://cdn.example/x", expect=RESPONSE, headers={name: "secret"})
        await client.get("x", expect=RESPONSE, headers={name: "secret"})

    foreign, home = web.sent
    assert name not in foreign.headers
    assert name in home.headers


async def test_a_default_header_credential_is_withheld_from_a_foreign_origin() -> None:
    """Withhold an ``Authorization`` configured in ``HttpOptions.headers`` from another origin."""
    web = Web()
    async with web.client(http=HttpOptions(headers={"Authorization": "Bearer configured"})) as client:
        await client.get("https://cdn.example/x", expect=RESPONSE)

    assert "Authorization" not in web.sent[0].headers


async def test_a_cookie_jar_scopes_cookies_itself() -> None:
    """Leave ``Cookie`` alone on a client that keeps a cookie jar, which scopes cookies to their domains."""
    web = Web()
    async with web.client(http=HttpOptions(cookies=True)) as client:
        await client.get("https://cdn.example/x", expect=RESPONSE, headers={"Cookie": "a=1"})

    assert web.sent[0].headers["Cookie"] == "a=1"


class _MintingAuth(CachedTokenAuth):
    """Mint by posting a basic-auth request to a token host on another origin, through the client."""

    @override
    async def _mint_flow(
        self, request: httpx2.Request
    ) -> AsyncGenerator[httpx2.Request | Minted[str], httpx2.Response]:
        response = yield httpx2.Request(
            "POST", "https://login.example/token", headers={"Authorization": "Basic Y2xpZW50OnNlY3JldA=="}
        )
        yield Minted(response.text, 3600.0)


@pytest.mark.parametrize(
    ("redirects", "keeps"),
    [
        ({}, [True]),
        ({"https://login.example/token": "https://login.example/v2/token"}, [True, True]),
        ({"https://login.example/token": "https://evil.example/steal"}, [True, False]),
    ],
    ids=["direct", "same-origin-redirect", "cross-origin-redirect"],
)
async def test_a_mint_hop_carries_its_credential_only_to_the_origin_it_named(
    redirects: dict[str, str], keeps: list[bool]
) -> None:
    """Let a mint hop carry its credential to its own token host, and to no other origin a redirect names."""
    web = Web(redirects)
    async with web.client(auth=_MintingAuth()) as client:
        await client.get("data", expect=RESPONSE)

    *mint_hops, call = web.sent
    assert ["Authorization" in hop.headers for hop in mint_hops] == keeps
    assert call.headers["Authorization"] == "Bearer ok"
    assert all(hop.extensions[CHAIN_ORIGIN] == "https://login.example" for hop in mint_hops)


@pytest.mark.parametrize("marker", [None, 1, "https://other.example"], ids=["absent", "not-text", "other-origin"])
def test_a_chain_marker_trusts_only_its_own_origin(marker: object) -> None:
    """Trust a request off the trusted origins only when its chain marker names exactly its origin."""
    guard = CredentialGuard(auth=None, base_url=httpx2.URL(BASE), trusted_origins=(), cookie_jar=False)
    extensions = {} if marker is None else {CHAIN_ORIGIN: marker}
    request = httpx2.Request("GET", "https://login.example/x", extensions=extensions)

    assert not guard.trusts(request)
    assert guard.trusts(
        httpx2.Request("GET", "https://login.example/x", extensions={CHAIN_ORIGIN: "https://login.example"})
    )


class _Signer:
    """A signing middleware that signs every attempt, wherever it goes, and declares what it stamps."""

    credential_headers: ClassVar = frozenset({"X-Signature"})
    credential_params: ClassVar = frozenset({"sig"})

    async def __call__(self, request: httpx2.Request, call_next: Next) -> httpx2.Response:
        """Stamp a signature header and query parameter, then send."""
        request.headers["X-Signature"] = "signed"
        request.url = request.url.copy_merge_params({"sig": "s"})
        return await call_next(request)


def _signed(request: httpx2.Request) -> set[str]:
    """Return which of :class:`_Signer`'s credentials *request* carries."""
    return {name for name in ("X-Signature", "sig") if name in request.headers or name in request.url.params}


async def test_a_middlewares_declared_credentials_stay_off_a_foreign_redirect_hop() -> None:
    """Withhold what a middleware declares from a hop to another origin, though it signs the hop after the hook ran."""
    web = Web({f"{BASE}/start": "https://files.example/signed"})
    async with web.client(http=HttpOptions(middleware=(_Signer(),))) as client:
        resp = await client.get("start", expect=RESPONSE)

    first, hop = web.sent
    assert resp.text == "ok"
    assert _signed(first) == {"X-Signature", "sig"}
    assert _signed(hop) == set()


async def test_a_middlewares_credentials_reach_a_trusted_origin() -> None:
    """Keep a middleware's credentials on a hop to an origin in ``trusted_origins``."""
    web = Web({f"{BASE}/start": "https://files.example/signed"})
    http = HttpOptions(middleware=(_Signer(),), trusted_origins=frozenset({"https://files.example"}))
    async with web.client(http=http) as client:
        await client.get("start", expect=RESPONSE)

    assert all(_signed(request) == {"X-Signature", "sig"} for request in web.sent)


async def test_a_middlewares_credentials_stay_off_a_mint_hops_token_host() -> None:
    """Withhold a middleware's credentials from a mint hop, whose origin is trusted for the mint's credentials only."""
    web = Web()
    async with web.client(auth=_MintingAuth(), http=HttpOptions(middleware=(_Signer(),))) as client:
        await client.get("data", expect=RESPONSE)

    mint_hop, call = web.sent
    assert "Authorization" in mint_hop.headers
    assert _signed(mint_hop) == set()
    assert _signed(call) == {"X-Signature", "sig"}
