"""A ``StaticHeaderAuth`` credential never leaves the client's origin, but the redirect is still followed."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from headgate._auth import StaticHeaderAuth
from headgate._base import AsyncBaseClient
from headgate._rate_limiter import Rate
from tests.support.responses import RESPONSE

if TYPE_CHECKING:
    from pytest_httpx import HTTPXMock


class _Client(AsyncBaseClient):
    """Minimal subclass."""


def _client(base_url: str = "https://api.example") -> _Client:
    return _Client(
        rate=Rate.per_second(100), base_url=base_url, auth=StaticHeaderAuth({"x-api-key": "k"}), max_retries=1
    )


@pytest.mark.parametrize(
    ("target", "kept"),
    [
        ("https://files.example/signed", False),
        ("http://api.example/x", False),
        ("https://api.example:8443/x", False),
        ("https://api.example/other", True),
    ],
)
async def test_credential_follows_only_same_origin_hops(httpx_mock: HTTPXMock, target: str, kept: bool) -> None:
    httpx_mock.add_response(url="https://api.example/start", status_code=302, headers={"location": target})
    httpx_mock.add_response(url=target, text="ok")
    response = await _client().get("start", expect=RESPONSE)
    assert response.text == "ok"
    hop = httpx_mock.get_requests()[-1]
    assert ("x-api-key" in hop.headers) is kept
