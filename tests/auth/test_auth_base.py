# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""``auth=`` takes a :class:`headgate.Auth`, which declares what it stamps, so no auth's credential leaves its origin.

A plain ``httpx2.Auth`` declares nothing, so the guard could not know which of its headers to withhold from another
origin: the client refuses it when it is built, ``httpx2.BasicAuth`` and ``httpx2.DigestAuth`` included.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar, cast, override

import httpx2
import pytest

import headgate
from headgate import (
    BearerAuth,
    CachedTokenAuth,
    Client,
    HttpOptions,
    QueryParamAuth,
    SigningAuth,
    StaticHeaderAuth,
    as_json,
)
from headgate.testing import FakeTransport, Respond

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


class _PlainKeyAuth(httpx2.Auth):
    """A plain httpx2 auth that stamps a key and declares nothing."""

    @override
    async def async_auth_flow(self, request: httpx2.Request) -> AsyncGenerator[httpx2.Request, httpx2.Response]:
        request.headers["X-Api-Key"] = "secret"
        yield request


class _KeyAuth(headgate.Auth):
    """A key auth that declares its header as a class attribute."""

    credential_headers: ClassVar = frozenset({"x-api-key"})
    credential_params: ClassVar = frozenset[str]()

    @override
    def credential_identity(self) -> bytes:
        return b"secret"

    @override
    async def async_auth_flow(self, request: httpx2.Request) -> AsyncGenerator[httpx2.Request, httpx2.Response]:
        request.headers["X-Api-Key"] = "secret"
        yield request


class _PropertyKeyAuth(headgate.Auth):
    """The same auth, declaring through properties, as headgate's own static auths do."""

    @property
    @override
    def credential_headers(self) -> frozenset[str]:
        return frozenset({"x-api-key"})

    @property
    @override
    def credential_params(self) -> frozenset[str]:
        return frozenset[str]()

    @override
    def credential_identity(self) -> bytes:
        return b"secret"

    @override
    async def async_auth_flow(self, request: httpx2.Request) -> AsyncGenerator[httpx2.Request, httpx2.Response]:
        request.headers["X-Api-Key"] = "secret"
        yield request


@pytest.mark.parametrize(
    "auth",
    [_PlainKeyAuth(), httpx2.BasicAuth("user", "pass"), httpx2.DigestAuth("user", "pass")],
    ids=["plain", "basic", "digest"],
)
def test_a_plain_httpx2_auth_is_refused(auth: httpx2.Auth) -> None:
    """Refuse an auth that declares nothing, naming it and what to write instead."""
    with pytest.raises(TypeError, match=r"auth= takes a headgate\.Auth") as raised:
        Client(base_url="https://api.example", auth=cast("Any", auth))
    assert type(auth).__qualname__ in str(raised.value)
    assert "StaticHeaderAuth" in str(raised.value)


@pytest.mark.parametrize("auth", [_KeyAuth(), _PropertyKeyAuth()], ids=["class-attribute", "property"])
async def test_a_declared_auth_is_withheld_cross_origin(auth: headgate.Auth) -> None:
    """Withhold a declared auth's own header from a redirect to another origin, and keep it on its own."""
    fake = (
        FakeTransport()
        .reply("GET", "/me", Respond(302, headers={"Location": "https://evil.example/steal"}))
        .reply("GET", "/steal", Respond(json={}))
    )
    async with Client(base_url="https://api.example", auth=auth, http=HttpOptions(transport=fake)) as client:
        await client.get("me", expect=as_json)

    assert [(call.url.host, call.headers.get("x-api-key")) for call in fake.calls] == [
        ("api.example", "secret"),
        ("evil.example", None),
    ]


def test_an_auth_missing_a_member_cannot_be_built() -> None:
    """Refuse to build an auth that leaves out whose credential it carries."""

    class Forgetful(headgate.Auth):
        credential_headers: ClassVar = frozenset({"x-api-key"})
        credential_params: ClassVar = frozenset[str]()

    with pytest.raises(TypeError, match="credential_identity"):
        cast("Any", Forgetful)()


@pytest.mark.parametrize("auth_class", [StaticHeaderAuth, BearerAuth, QueryParamAuth, SigningAuth, CachedTokenAuth])
def test_every_headgate_auth_is_an_auth(auth_class: type) -> None:
    """Derive every auth headgate ships from the one base an auth is."""
    assert issubclass(auth_class, headgate.Auth)
