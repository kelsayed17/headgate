# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""``CassetteFilter`` with the real vcrpy: what a recorded cassette holds, and that it replays.

vcrpy patches httpx2's own transports (``AsyncHTTPTransport`` and ``MockTransport``), below headgate's stack, so the
network here is an ``httpx2.MockTransport`` and every send that reaches it is recorded, except the ones the filter
skips.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, override

import httpx2
import vcr

from headgate import CachedTokenAuth, HttpOptions, Minted, Rate, as_bytes
from headgate.testing import CassetteFilter
from tests.support.clients import StubClient
from tests.support.tokens import premint

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from pathlib import Path

_BASE = "https://api.example.test"
_MINT_URL = "https://auth.example.test/token"

#: The secrets the exchange involves: the long-lived key the mint sends, the token it returns, the session cookie.
_API_KEY = "long-lived-api-key"
_MINTED = "minted-access-value"
_SESSION = "session-cookie-value"

#: The body the endpoint answers with.
_BODY = b"the endpoint's answer"


class _MintingAuth(CachedTokenAuth):
    """A bearer minted through the client's own stack, from an API key sent in the mint's body."""

    @override
    async def _mint_flow(
        self, request: httpx2.Request
    ) -> AsyncGenerator[httpx2.Request | Minted[str], httpx2.Response]:
        del request
        response = yield httpx2.Request("POST", _MINT_URL, json={"api_key": _API_KEY})
        yield Minted(response.json()["token"], 3600.0)


def _network(request: httpx2.Request) -> httpx2.Response:
    """Answer the mint with a token, and the endpoint with its body and a session cookie."""
    if request.url == _MINT_URL:
        return httpx2.Response(200, json={"token": _MINTED})
    return httpx2.Response(200, content=_BODY, headers={"Set-Cookie": f"session={_SESSION}"})


def _refuse(request: httpx2.Request) -> httpx2.Response:
    """Fail the test if a replayed call reaches the network."""
    msg = f"{request.method} {request.url} reached the network during replay"
    raise AssertionError(msg)


def _client(auth: CachedTokenAuth, cassettes: CassetteFilter, network: httpx2.MockTransport) -> StubClient:
    """Build a client signing with *auth* over *network*, with *cassettes* marking the auth's mints."""
    return StubClient(
        base_url=_BASE,
        rate=Rate.per_second(1000),
        auth=auth,
        http=HttpOptions(transport=network, middleware=(cassettes,)),
    )


async def test_a_cassette_holds_the_call_and_no_credential(tmp_path: Path) -> None:
    """Record the call itself, not the mint; keep the bearer, the key, the token and the cookie out of the file."""
    cassettes = CassetteFilter(auths=(_MintingAuth(),))
    cassette = tmp_path / "call.yaml"

    with vcr.VCR(**cassettes.vcr_config()).use_cassette(str(cassette), record_mode="all"):
        body = await _client(_MintingAuth(), cassettes, httpx2.MockTransport(_network)).get("data", expect=as_bytes)

    recorded = cassette.read_text()
    assert body == _BODY
    assert f"{_BASE}/data" in recorded
    assert _BODY.decode() in recorded
    for secret in (_API_KEY, _MINTED, _SESSION, _MINT_URL):
        assert secret not in recorded


async def test_a_cassette_replays_without_the_network(tmp_path: Path) -> None:
    """Replay the recorded answer to a client whose auth is seeded, as the recipe says, never touching the network."""
    cassettes = CassetteFilter(auths=(_MintingAuth(),))
    cassette = tmp_path / "call.yaml"
    recorder = vcr.VCR(**cassettes.vcr_config())
    with recorder.use_cassette(str(cassette), record_mode="all"):
        await _client(_MintingAuth(), cassettes, httpx2.MockTransport(_network)).get("data", expect=as_bytes)

    with recorder.use_cassette(str(cassette), record_mode="none"):
        replayed = await _client(premint(_MintingAuth()), cassettes, httpx2.MockTransport(_refuse)).get(
            "data", expect=as_bytes
        )

    assert replayed == _BODY
