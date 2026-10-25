"""Tests for the generic httpx2.Auth family in headgate._auth.

Each Auth subclass replaces a per-request auth hook / per-call header|param
injection. The load-bearing invariant is **explicit-header/param precedence**:
a caller-supplied ``headers=``/``params=`` value must still win a clash with the
Auth flow (reproducing the pre-migration ``_request_auth_headers`` merge, which
put auth UNDER explicit headers). All Auth classes therefore use a
setdefault-style merge.

Provider-specific Auth classes are tested in their provider test dirs:
``ExampleCDPAuth`` in ``tests/clients/example/test_auth.py`` and
``FinraOAuth`` in ``tests/clients/finra/test_auth.py`` — keeping this
module (and the shared ``_auth`` it tests) provider-agnostic.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from headgate._auth import QueryParamAuth, StaticHeaderAuth
from headgate._base import AsyncBaseClient
from headgate._rate_limit import Rate
from tests.support.responses import RESPONSE

if TYPE_CHECKING:
    from pytest_httpx import HTTPXMock


class _StubClient(AsyncBaseClient):
    """Minimal AsyncBaseClient subclass for wiring an Auth under test."""


# ---------- StaticHeaderAuth ----------


async def test_static_header_auth_sets_header(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(url="https://api.example/x", json={})
    client = _StubClient(
        rate=Rate.per_second(2),
        base_url="https://api.example",
        auth=StaticHeaderAuth({"Authorization": "Bearer T"}),
    )
    await client.get("x", expect=RESPONSE)
    req = httpx_mock.get_requests()[0]
    assert req.headers["Authorization"] == "Bearer T"


async def test_static_header_auth_explicit_header_wins(httpx_mock: HTTPXMock) -> None:
    """An explicit ``headers=`` value wins over the Auth flow (setdefault)."""
    httpx_mock.add_response(url="https://api.example/x", json={})
    client = _StubClient(
        rate=Rate.per_second(2),
        base_url="https://api.example",
        auth=StaticHeaderAuth({"Authorization": "Bearer T"}),
    )
    await client.get("x", expect=RESPONSE, headers={"Authorization": "X"})
    req = httpx_mock.get_requests()[0]
    assert req.headers["Authorization"] == "X"


async def test_static_header_auth_empty_is_noop(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(url="https://api.example/x", json={})
    client = _StubClient(rate=Rate.per_second(2), base_url="https://api.example", auth=StaticHeaderAuth({}))
    await client.get("x", expect=RESPONSE)
    req = httpx_mock.get_requests()[0]
    assert "Authorization" not in req.headers


# ---------- QueryParamAuth ----------


async def test_query_param_auth_merges_params(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(json={})
    client = _StubClient(rate=Rate.per_second(2), base_url="https://api.example", auth=QueryParamAuth({"api_key": "K"}))
    await client.get("x", expect=RESPONSE, params={"series": "Y"})
    req = httpx_mock.get_requests()[0]
    assert req.url.params["api_key"] == "K"
    assert req.url.params["series"] == "Y"


async def test_query_param_auth_explicit_param_wins(httpx_mock: HTTPXMock) -> None:
    """A caller-supplied ``api_key`` param survives — setdefault, not overwrite."""
    httpx_mock.add_response(json={})
    client = _StubClient(rate=Rate.per_second(2), base_url="https://api.example", auth=QueryParamAuth({"api_key": "K"}))
    await client.get("x", expect=RESPONSE, params={"api_key": "CALLER"})
    req = httpx_mock.get_requests()[0]
    assert req.url.params["api_key"] == "CALLER"
