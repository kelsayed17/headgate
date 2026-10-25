"""A ``StaticHeaderAuth`` credential never leaves the client's origin, but the redirect is still followed."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from headgate._auth import StaticHeaderAuth
from headgate._rate_limit import Rate
from tests.support.clients import StubClient
from tests.support.responses import RESPONSE

if TYPE_CHECKING:
    from pytest_httpx2 import HTTPXMock


def _client() -> StubClient:
    return StubClient(
        rate=Rate.per_second(100),
        base_url="https://api.example",
        auth=StaticHeaderAuth({"x-api-key": "k"}),
        max_attempts=1,
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
async def test_credential_follows_only_same_origin_hops(httpx2_mock: HTTPXMock, target: str, kept: bool) -> None:
    httpx2_mock.add_response(url="https://api.example/start", status_code=302, headers={"location": target})
    httpx2_mock.add_response(url=target, text="ok")
    response = await _client().get("start", expect=RESPONSE)
    assert response.text == "ok"
    hop = httpx2_mock.get_requests()[-1]
    assert ("x-api-key" in hop.headers) is kept
