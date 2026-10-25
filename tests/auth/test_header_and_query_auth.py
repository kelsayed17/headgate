# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""``StaticHeaderAuth`` and ``QueryParamAuth``: a fixed credential merged into every request.

The invariant both pin is explicit-value precedence: a caller's ``headers=`` / ``params=`` wins a clash with the auth
flow, because each class merges setdefault-style rather than assigning.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from headgate._auth import QueryParamAuth, StaticHeaderAuth
from headgate._rate_limit import Rate
from tests.support.clients import StubClient
from tests.support.responses import RESPONSE

if TYPE_CHECKING:
    from pytest_httpx2 import HTTPXMock


# --- StaticHeaderAuth ---


async def test_static_header_auth_sets_header(httpx2_mock: HTTPXMock) -> None:
    httpx2_mock.add_response(url="https://api.example/x", json={})
    client = StubClient(
        rate=Rate.per_second(2),
        base_url="https://api.example",
        auth=StaticHeaderAuth({"Authorization": "Bearer T"}),
    )
    await client.get("x", expect=RESPONSE)
    req = httpx2_mock.get_requests()[0]
    assert req.headers["Authorization"] == "Bearer T"


async def test_static_header_auth_explicit_header_wins(httpx2_mock: HTTPXMock) -> None:
    """An explicit ``headers=`` value wins over the Auth flow (setdefault)."""
    httpx2_mock.add_response(url="https://api.example/x", json={})
    client = StubClient(
        rate=Rate.per_second(2),
        base_url="https://api.example",
        auth=StaticHeaderAuth({"Authorization": "Bearer T"}),
    )
    await client.get("x", expect=RESPONSE, headers={"Authorization": "X"})
    req = httpx2_mock.get_requests()[0]
    assert req.headers["Authorization"] == "X"


async def test_static_header_auth_empty_is_noop(httpx2_mock: HTTPXMock) -> None:
    httpx2_mock.add_response(url="https://api.example/x", json={})
    client = StubClient(rate=Rate.per_second(2), base_url="https://api.example", auth=StaticHeaderAuth({}))
    await client.get("x", expect=RESPONSE)
    req = httpx2_mock.get_requests()[0]
    assert "Authorization" not in req.headers


# --- QueryParamAuth ---


async def test_query_param_auth_merges_params(httpx2_mock: HTTPXMock) -> None:
    httpx2_mock.add_response(json={})
    client = StubClient(rate=Rate.per_second(2), base_url="https://api.example", auth=QueryParamAuth({"api_key": "K"}))
    await client.get("x", expect=RESPONSE, params={"series": "Y"})
    req = httpx2_mock.get_requests()[0]
    assert req.url.params["api_key"] == "K"
    assert req.url.params["series"] == "Y"


async def test_query_param_auth_explicit_param_wins(httpx2_mock: HTTPXMock) -> None:
    """A caller-supplied ``api_key`` param survives — setdefault, not overwrite."""
    httpx2_mock.add_response(json={})
    client = StubClient(rate=Rate.per_second(2), base_url="https://api.example", auth=QueryParamAuth({"api_key": "K"}))
    await client.get("x", expect=RESPONSE, params={"api_key": "CALLER"})
    req = httpx2_mock.get_requests()[0]
    assert req.url.params["api_key"] == "CALLER"
