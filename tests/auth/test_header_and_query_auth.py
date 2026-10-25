# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""``StaticHeaderAuth`` and ``QueryParamAuth``: a fixed credential merged into every request.

The invariant both pin is explicit-value precedence: a caller's ``headers=`` / ``params=`` wins a clash with the auth
flow, because each class merges setdefault-style rather than assigning.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Unpack

from headgate._auth import QueryParamAuth, StaticHeaderAuth
from headgate._rate_limit import Rate
from headgate._url import is_secret_param
from tests.support.clients import StubClient
from tests.support.responses import RESPONSE

if TYPE_CHECKING:
    import httpx2
    from pytest_httpx2 import HTTPXMock

    from headgate._client import RequestOptions


async def _sent(httpx2_mock: HTTPXMock, auth: httpx2.Auth, **options: Unpack[RequestOptions]) -> httpx2.Request:
    """Send one GET through a client using *auth*, with the call *options*, and return the request that went out."""
    httpx2_mock.add_response(json={})
    client = StubClient(rate=Rate.per_second(100), base_url="https://api.example", auth=auth)
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

    async def test_an_empty_mapping_is_a_no_op(self, httpx2_mock: HTTPXMock) -> None:
        """Send the request unchanged when no header is configured."""
        req = await _sent(httpx2_mock, StaticHeaderAuth({}))

        assert "Authorization" not in req.headers

    def test_the_header_names_are_what_the_redirect_guard_withholds(self) -> None:
        """Expose the configured names, copied so a later change to the caller's mapping does not reach the auth."""
        headers = {"x-api-key": "k"}
        auth = StaticHeaderAuth(headers)
        headers["x-other"] = "v"

        assert auth.header_names == {"x-api-key"}


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
        name = "headgate_test_credential"
        assert not is_secret_param(name)

        QueryParamAuth({name: "v"})

        assert is_secret_param(name)
