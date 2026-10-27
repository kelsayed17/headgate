# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""Every auth headgate ships is a ``CredentialStamper``: it declares what it stamps and names its credential.

What an auth declares must be what it puts on a request, since the credential guard withholds exactly that and the
cache partitions and strips by it. ``credential_identity()`` must be stable for one credential and differ between two,
and for a ``CachedTokenAuth`` it names the long-lived credential, never the token, which changes on every refresh.
"""

from __future__ import annotations

import math
import uuid
from typing import TYPE_CHECKING, ClassVar, override

import httpx2
import pytest

from headgate import BearerAuth, CachedTokenAuth, QueryParamAuth, StaticHeaderAuth
from headgate._core import CredentialStamper
from tests.support.tokens import premint

if TYPE_CHECKING:
    from collections.abc import Callable


class _Bearer(CachedTokenAuth):
    """A bearer auth whose exchange never runs."""

    @override
    async def mint_token(self) -> tuple[str, float]:
        raise NotImplementedError


class _KeyedBearer(_Bearer):
    """A bearer auth that names its long-lived credential, so two instances holding it share an identity."""

    def __init__(self, key: str) -> None:
        """Hold the long-lived *key*."""
        super().__init__()
        self._key = key

    @override
    def credential_identity(self) -> bytes:
        return self._key.encode()


class _PairToken(_Bearer):
    """A credential riding two headers, declared in ``token_headers``."""

    token_headers: ClassVar = frozenset({"X-Cookie", "X-Crumb"})

    @override
    def attach(self, request: httpx2.Request, token: str) -> None:
        request.headers["X-Cookie"], request.headers["X-Crumb"] = token, token


class _InheritsAttach(_PairToken):
    """A subclass of a declaring class, which declares nothing itself."""


class _DeclaredByProperty(_Bearer):
    """A custom ``attach`` declared by overriding ``credential_headers`` instead of ``token_headers``."""

    @property
    @override
    def credential_headers(self) -> frozenset[str]:
        return frozenset({"x-signed"})

    @override
    def attach(self, request: httpx2.Request, token: str) -> None:
        request.headers["X-Signed"] = token


@pytest.mark.parametrize(
    "auth",
    [StaticHeaderAuth({}), QueryParamAuth({}), _Bearer()],
    ids=["static-header", "query-param", "cached-token"],
)
def test_every_auth_is_a_credential_stamper(auth: httpx2.Auth) -> None:
    """Satisfy the runtime-checkable protocol the guard and the cache test for."""
    assert isinstance(auth, CredentialStamper)


def test_the_query_auth_declares_its_parameters() -> None:
    """Declare the configured parameter names and no headers."""
    # Each name is registered as secret process-wide, so the second is unique to this run.
    name = f"headgate_test_{uuid.uuid4().hex}"
    auth = QueryParamAuth({"api_key": "k", name: "s"})

    assert auth.credential_params == {"api_key", name}
    assert auth.credential_headers == frozenset()


@pytest.mark.parametrize(
    ("auth", "headers"),
    [
        (_Bearer(), {"authorization"}),
        (_PairToken(), {"x-cookie", "x-crumb"}),
        (_InheritsAttach(), {"x-cookie", "x-crumb"}),
    ],
    ids=["default", "declared", "inherited"],
)
async def test_a_token_auth_declares_exactly_what_it_stamps(auth: CachedTokenAuth, headers: set[str]) -> None:
    """Report, lower-cased, exactly the headers ``attach`` puts on a request, and no parameters."""
    premint(auth)
    request = httpx2.Request("GET", "https://api.example/x")
    flow = auth.async_auth_flow(request)
    stamped = await anext(flow)
    await flow.aclose()

    assert auth.credential_headers == headers
    assert {name.lower() for name in stamped.headers} - {"host"} == headers
    assert auth.credential_params == frozenset()


def test_a_token_auth_may_declare_through_credential_headers() -> None:
    """Accept a custom ``attach`` whose class overrides ``credential_headers`` rather than ``token_headers``."""
    assert _DeclaredByProperty().credential_headers == {"x-signed"}


def test_an_undeclared_attach_fails_when_the_class_is_defined() -> None:
    """Refuse a subclass whose ``attach`` sets headers it has not declared, naming the fix."""
    with pytest.raises(TypeError, match="token_headers"):

        class _Undeclared(_Bearer):
            @override
            def attach(self, request: httpx2.Request, token: str) -> None:
                request.headers["X-Secret"] = token

    assert _Bearer().credential_headers == {"authorization"}


def test_the_declaration_is_checked_where_attach_is_overridden() -> None:
    """Accept ``token_headers`` or ``credential_headers`` on the overriding class; refuse it only on a parent.

    The classes are defined inside the test, so the check runs while the test does.
    """

    class _ByTokenHeaders(_Bearer):
        token_headers: ClassVar = frozenset({"X-One"})

        @override
        def attach(self, request: httpx2.Request, token: str) -> None:
            request.headers["X-One"] = token

    class _ByProperty(_Bearer):
        @property
        @override
        def credential_headers(self) -> frozenset[str]:
            return frozenset({"x-two"})

        @override
        def attach(self, request: httpx2.Request, token: str) -> None:
            request.headers["X-Two"] = token

    class _Inherits(_ByTokenHeaders):
        """Inherit the declaring class's ``attach`` and its declaration."""

    assert (_ByTokenHeaders().credential_headers, _ByProperty().credential_headers) == ({"x-one"}, {"x-two"})
    assert _Inherits().credential_headers == {"x-one"}
    with pytest.raises(TypeError, match=r"_Overrides overrides attach, so it must declare"):

        class _Overrides(_ByTokenHeaders):
            @override
            def attach(self, request: httpx2.Request, token: str) -> None:
                request.headers["X-Three"] = token


async def test_the_default_skip_looks_for_any_token_header() -> None:
    """Pass a request already carrying one of the declared token headers through untouched, without minting."""
    request = httpx2.Request("GET", "https://api.example/", headers={"x-crumb": "mine"})
    flow = _PairToken().async_auth_flow(request)
    sent = await anext(flow)
    await flow.aclose()

    assert sent.headers["X-Crumb"] == "mine"
    assert "X-Cookie" not in sent.headers


class TestIdentity:
    """``credential_identity()``: stable for one credential, different for two, never the short-lived token."""

    @pytest.mark.parametrize(
        ("same", "other"),
        [
            (
                lambda: StaticHeaderAuth({"X-Api-Key": "k", "X-Tenant": "t"}),
                lambda: StaticHeaderAuth({"x-api-key": "other", "X-Tenant": "t"}),
            ),
            (lambda: QueryParamAuth({"api_key": "k"}), lambda: QueryParamAuth({"api_key": "kk"})),
        ],
        ids=["static-header", "query-param"],
    )
    def test_a_static_credential_is_identified_by_its_material(
        self, same: Callable[[], CredentialStamper], other: Callable[[], CredentialStamper]
    ) -> None:
        """Give the same credential the same bytes, built twice, and a different credential different bytes."""
        assert same().credential_identity() == same().credential_identity()
        assert same().credential_identity() != other().credential_identity()

    def test_header_names_are_matched_case_insensitively(self) -> None:
        """Treat a header name differing only in case as the same credential."""
        assert (
            StaticHeaderAuth({"X-Api-Key": "k"}).credential_identity()
            == StaticHeaderAuth({"x-api-key": "k"}).credential_identity()
        )

    def test_a_static_header_identity_is_a_netstring_of_the_lower_cased_pairs(self) -> None:
        """Encode each name, lower-cased, and value as a netstring, the pairs in sorted order."""
        auth = StaticHeaderAuth({"X-Tenant": "t", "X-Api-Key": "k"})

        assert auth.credential_identity() == b"9:x-api-key,1:k,8:x-tenant,1:t,"

    def test_the_encoding_cannot_be_confused_by_shifting_text_between_parts(self) -> None:
        """Keep a name-value boundary in the bytes, so ``{"ab": "c"}`` and ``{"a": "bc"}`` differ."""
        prefix = f"headgate_test_{uuid.uuid4().hex}_"
        shifted = QueryParamAuth({prefix + "ab": "c"}), QueryParamAuth({prefix + "a": "bc"})

        assert shifted[0].credential_identity() != shifted[1].credential_identity()

    def test_a_token_auth_is_its_own_identity_by_default(self) -> None:
        """Give each instance its own stable identity, unrelated to the token it holds."""
        auth = premint(_Bearer(), "token-1")
        before = auth.credential_identity()
        auth.seed("token-2", math.inf)

        assert auth.credential_identity() == before
        assert _Bearer().credential_identity() != before
        assert b"token" not in before

    def test_a_token_auth_names_its_long_lived_credential_when_it_declares_one(self) -> None:
        """Share one identity between instances holding the same long-lived credential, whatever their tokens."""
        first, second = premint(_KeyedBearer("key-1"), "a"), premint(_KeyedBearer("key-1"), "b")

        assert first.credential_identity() == second.credential_identity()
        assert first.credential_identity() != _KeyedBearer("key-2").credential_identity()


async def test_a_bearer_auth_is_the_static_bearer_header() -> None:
    """Stamp ``Authorization: Bearer <token>`` and name the same credential as the header written out by hand."""
    auth = BearerAuth("tok")
    sent = await anext(auth.async_auth_flow(httpx2.Request("GET", "https://api.example/x")))
    assert sent.headers["Authorization"] == "Bearer tok"
    assert auth.credential_headers == {"authorization"}
    assert auth.credential_identity() == StaticHeaderAuth({"Authorization": "Bearer tok"}).credential_identity()
