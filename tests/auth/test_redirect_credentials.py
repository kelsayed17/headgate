# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""A ``StaticHeaderAuth`` credential never leaves the client's origin, but the redirect is still followed."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from headgate import TransportPolicy
from headgate._auth import StaticHeaderAuth
from headgate.ratelimit import Rate
from headgate.retry import RetryPolicy
from tests.support.clients import StubClient
from tests.support.responses import RESPONSE

if TYPE_CHECKING:
    import httpx2
    from pytest_httpx2 import HTTPXMock


async def _follow_redirect_to(httpx2_mock: HTTPXMock, target: str) -> httpx2.Request:
    """Redirect a call to *target*, check the redirect was followed, and return the request sent there."""
    httpx2_mock.add_response(url="https://api.example/start", status_code=302, headers={"location": target})
    httpx2_mock.add_response(url=target, text="ok")
    client = StubClient(
        rate=Rate.per_second(100),
        base_url="https://api.example",
        auth=StaticHeaderAuth({"x-api-key": "k"}),
        policy=TransportPolicy(retry=RetryPolicy(attempts=1)),
    )
    response = await client.get("start", expect=RESPONSE)
    assert response.text == "ok"
    return httpx2_mock.get_requests()[-1]


@pytest.mark.parametrize(
    "target",
    ["https://files.example/signed", "http://api.example/x", "https://api.example:8443/x"],
    ids=["other-host", "other-scheme", "other-port"],
)
async def test_a_cross_origin_hop_is_followed_without_the_credential(httpx2_mock: HTTPXMock, target: str) -> None:
    """Withhold the header from a hop to another host, scheme or port, and still follow it."""
    hop = await _follow_redirect_to(httpx2_mock, target)

    assert "x-api-key" not in hop.headers


async def test_a_same_origin_hop_keeps_the_credential(httpx2_mock: HTTPXMock) -> None:
    """Keep the header on a hop that stays on the client's origin."""
    hop = await _follow_redirect_to(httpx2_mock, "https://api.example/other")

    assert hop.headers["x-api-key"] == "k"
