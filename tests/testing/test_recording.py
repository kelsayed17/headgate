# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""``CassetteFilter``: vcrpy filters built from headgate's secrets, and auth hops kept out of the cassette."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, override

import httpx2

from headgate import CachedTokenAuth, HttpOptions, Minted, Rate, as_bytes
from headgate.testing import CassetteFilter, FakeTransport
from tests.support.clients import StubClient

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

_BASE = "https://api.example.test"
_MINT_URL = "https://auth.example.test/token"


@dataclass
class _VcrRequest:
    """The part of a vcrpy request the filter reads and rewrites."""

    uri: str


class _Stamper:
    """An auth that declares the credentials it stamps."""

    @property
    def credential_headers(self) -> frozenset[str]:
        """Name the header it sets."""
        return frozenset({"X-Api-Key"})

    @property
    def credential_params(self) -> frozenset[str]:
        """Name the query parameter it sets."""
        return frozenset({"appid"})

    def credential_identity(self) -> bytes:
        """Return the secret."""
        return b"secret"


def test_the_config_filters_the_credential_headers_and_what_auths_declare() -> None:
    """Filter the standard credential headers, the stamper's names, and the names given."""
    config = CassetteFilter(auths=(_Stamper(), object()), headers=("X-Session",), params=("user_key",)).vcr_config()

    assert config["filter_headers"] == ["authorization", "cookie", "proxy-authorization", "x-api-key", "x-session"]
    assert config["filter_query_parameters"] == ["appid", "user_key"]


def test_a_recorded_request_has_its_secret_query_values_redacted() -> None:
    """Redact every well-known or registered secret parameter and the userinfo, keeping the rest of the URI."""
    cassettes = CassetteFilter()
    recorded = cassettes.before_record_request(_VcrRequest("https://user:pw@h.test/x?api_key=s3cret&page=2"))

    assert recorded is not None
    assert recorded.uri == "https://***@h.test/x?api_key=%2A%2A%2A&page=2"


def test_a_recorded_response_loses_its_cookies() -> None:
    """Drop ``Set-Cookie`` from the recorded response and keep every other header."""
    response: dict[str, object] = {"headers": {"Set-Cookie": ["session=1"], "Content-Type": ["text/plain"]}}
    assert CassetteFilter().before_record_response(response) == {"headers": {"Content-Type": ["text/plain"]}}


def test_a_response_without_headers_is_left_alone() -> None:
    """Return a recorded response that has no header mapping unchanged."""
    response: dict[str, object] = {"status": {"code": 200}}
    assert CassetteFilter().before_record_response(response) == {"status": {"code": 200}}


class _FlowAuth(CachedTokenAuth):
    """A token auth that mints through the client's own stack, so its hop passes the middleware."""

    @override
    async def _mint_flow(
        self, request: httpx2.Request
    ) -> AsyncGenerator[httpx2.Request | Minted[str], httpx2.Response]:
        del request
        response = yield httpx2.Request("POST", _MINT_URL)
        yield Minted(response.json()["token"], 3600.0)


async def test_a_token_mint_is_skipped_and_the_call_itself_is_recorded() -> None:
    """Return ``None`` for the hop an auth flow sends to mint, and the request for the call it was minted for."""
    cassettes = CassetteFilter()
    recorded: dict[str, bool] = {}

    def token(request: httpx2.Request) -> httpx2.Response:
        recorded["mint"] = cassettes.before_record_request(_VcrRequest(str(request.url))) is not None
        return httpx2.Response(200, json={"token": "t"})

    def answer(request: httpx2.Request) -> httpx2.Response:
        recorded["call"] = cassettes.before_record_request(_VcrRequest(str(request.url))) is not None
        return httpx2.Response(200, content=b"ok")

    fake = FakeTransport().route("POST", "/token", token).route("GET", "/data", answer)
    client = StubClient(
        base_url=_BASE,
        rate=Rate.per_second(1000),
        auth=_FlowAuth(),
        http=HttpOptions(transport=fake, middleware=(cassettes,)),
    )
    assert await client.get("data", expect=as_bytes) == b"ok"
    assert recorded == {"mint": False, "call": True}
