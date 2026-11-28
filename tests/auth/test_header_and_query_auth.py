# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""``StaticHeaderAuth`` and ``QueryParamAuth``: a fixed credential merged into every request.

The invariant both pin is explicit-value precedence: a caller's ``headers=`` / ``params=`` wins a clash with the auth
flow, because each class merges setdefault-style rather than assigning.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any, Unpack, cast

import pytest

from headgate import Auth, BearerAuth, CallPolicy, Client, Rate
from headgate._auth import QueryParamAuth, StaticHeaderAuth
from headgate._url import is_secret_param
from tests.support.responses import RESPONSE

if TYPE_CHECKING:
    from collections.abc import Callable

    import httpx2
    from pytest_httpx2 import HTTPXMock

    from headgate import CallOptions


async def _sent(httpx2_mock: HTTPXMock, auth: Auth, **options: Unpack[CallOptions]) -> httpx2.Request:
    """Send one GET through a client using *auth*, with the call *options*, and return the request that went out."""
    httpx2_mock.add_response(json={})
    client = Client(policy=CallPolicy(rate=Rate.per_second(100)), base_url="https://api.example", auth=auth)
    await client.get("x", expect=RESPONSE, **options)
    return httpx2_mock.get_requests()[0]


class TestStaticHeaderAuth:
    """``StaticHeaderAuth`` stamps its headers unless the caller set them."""

    async def test_the_header_is_set(self, httpx2_mock: HTTPXMock) -> None:
        """Stamp the configured header."""
        req = await _sent(httpx2_mock, StaticHeaderAuth({"Authorization": "Bearer T"}))

        assert req.headers["Authorization"] == "Bearer T"

    async def test_an_explicit_header_wins_whatever_its_case(self, httpx2_mock: HTTPXMock) -> None:
        """Keep the caller's header, matched case-insensitively, over the auth's."""
        req = await _sent(httpx2_mock, StaticHeaderAuth({"Authorization": "Bearer T"}), headers={"authorization": "X"})

        assert req.headers.get_list("Authorization") == ["X"]

    def test_the_credential_headers_are_the_configured_names(self) -> None:
        """Declare the configured names, copied so a later change to the caller's mapping does not reach the auth."""
        headers = {"X-Api-Key": "k"}
        auth = StaticHeaderAuth(headers)
        headers["x-other"] = "v"

        assert auth.credential_headers == {"x-api-key"}
        assert auth.credential_params == frozenset()


class TestQueryParamAuth:
    """``QueryParamAuth`` merges its params unless the caller set them, and marks their names secret."""

    async def test_the_params_are_merged(self, httpx2_mock: HTTPXMock) -> None:
        """Add the configured param alongside the caller's."""
        req = await _sent(httpx2_mock, QueryParamAuth({"api_key": "K"}), params={"series": "Y"})

        assert req.url.params["api_key"] == "K"
        assert req.url.params["series"] == "Y"

    async def test_an_explicit_param_wins(self, httpx2_mock: HTTPXMock) -> None:
        """Keep a caller-supplied ``api_key``: setdefault, not overwrite."""
        req = await _sent(httpx2_mock, QueryParamAuth({"api_key": "K"}), params={"api_key": "CALLER"})

        assert req.url.params.get_list("api_key") == ["CALLER"]

    def test_the_param_names_are_registered_as_secret(self) -> None:
        """Register a name the well-known list lacks, so error messages and the cache redact it."""
        # The registry is process-wide and add-only, so a name unique to this run keeps the test independent of order.
        name = f"headgate_test_credential_{uuid.uuid4().hex}"
        assert not is_secret_param(name)

        QueryParamAuth({name: "v"})

        assert is_secret_param(name)


@pytest.mark.parametrize("token", ["", "   ", "\t"], ids=["empty", "blank", "tab"])
def test_a_bearer_token_is_not_empty(token: str) -> None:
    """Refuse an empty or blank token, the unset environment variable, which would send ``Bearer `` alone."""
    with pytest.raises(ValueError, match=r"^BearerAuth\(token=\) takes a token that is not empty or blank"):
        BearerAuth(token)


@pytest.mark.parametrize(
    "token", ["Bearer abc", "bearer abc", "BEARER abc", "bearer\tabc", "Bearer", "BEARER", " Bearer abc", " bearer "]
)
def test_a_bearer_token_does_not_repeat_the_scheme(token: str) -> None:
    """Refuse a token that starts with the scheme, in any case, which would send ``Bearer Bearer abc``."""
    with pytest.raises(
        ValueError, match=r"^BearerAuth\(token=\) takes the token alone, without its 'Bearer' scheme"
    ) as raised:
        BearerAuth(token)
    assert "abc" not in str(raised.value), "the token's value is never shown"


def test_a_token_that_only_begins_with_the_letters_of_the_scheme_is_accepted() -> None:
    """Accept ``bearertoken``: only the scheme as a word, alone or followed by whitespace, is the doubled scheme."""
    assert b"Bearer bearertoken" in BearerAuth("bearertoken").credential_identity()


@pytest.mark.parametrize("token", [" abc", "abc\n", "abc "], ids=["leading", "newline", "trailing"])
def test_a_bearer_token_has_no_surrounding_whitespace(token: str) -> None:
    """Refuse a token with whitespace around it, a copy-paste or a file read without stripping, never naming it."""
    with pytest.raises(ValueError, match=r"^BearerAuth\(token=\) takes a token without whitespace around it") as raised:
        BearerAuth(token)
    assert "abc" not in str(raised.value)


def test_a_bearer_token_is_text() -> None:
    """Refuse a token that is not a ``str``."""
    with pytest.raises(TypeError, match=r"^token= takes the token, a str \(got bytes\)$"):
        BearerAuth(cast("Any", b"abc"))


@pytest.mark.parametrize("auth", [StaticHeaderAuth, QueryParamAuth], ids=["headers", "params"])
def test_a_static_auth_stamps_something(auth: type[StaticHeaderAuth | QueryParamAuth]) -> None:
    """Refuse an empty mapping, which stamps nothing: a client with no key passes ``auth=None``."""
    with pytest.raises(ValueError, match=rf"^{auth.__name__} needs at least one .*: pass auth=None"):
        auth({})


@pytest.mark.parametrize(
    ("build", "message"),
    [
        (lambda: QueryParamAuth(cast("Any", "key")), r"^params= takes a mapping of parameter names to str values"),
        (lambda: StaticHeaderAuth(cast("Any", [("X", "1")])), r"^headers= takes a mapping of header names to str"),
        (lambda: StaticHeaderAuth(cast("Any", {"X-Key": 1})), r"\(got int under 'X-Key'\)$"),
        (lambda: QueryParamAuth(cast("Any", {"key": 1})), r"\(got int under 'key'\)$"),
    ],
    ids=["param-text", "header-pairs", "header-int", "param-int"],
)
def test_a_static_auth_takes_a_mapping_of_text(build: Callable[[], object], message: str) -> None:
    """Refuse pairs, a bare ``str`` and a value that is not a ``str``, naming the option."""
    with pytest.raises(TypeError, match=message):
        build()
