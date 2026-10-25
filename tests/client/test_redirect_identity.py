# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""A capped response reports the URL that answered, not the one that was sent.

``AsyncBaseClient`` rebuilds every response after capping its body (``read_capped``), and httpx2 derives
``Response.url`` from the request the response carries. The rebuild must therefore attach the final hop's request:
with the original one attached, a redirected response would claim the URL it was sent to and be indistinguishable,
to a caller, from one that was never redirected. A caller that detects a bad credential by where a redirect landed
(an HTML landing page served as a 200 rather than a 401) depends on this.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx2

from headgate._client import AsyncBaseClient
from headgate.ratelimit import Rate
from tests.support.responses import RESPONSE

if TYPE_CHECKING:
    from pytest_httpx2 import HTTPXMock


class _Client(AsyncBaseClient):
    """A client that follows redirects, the default."""

    def __init__(self) -> None:
        """Build against ``example.test``."""
        super().__init__(base_url="https://example.test", rate=Rate.per_second(1000.0))


def _redirecting(httpx2_mock: HTTPXMock) -> None:
    """Register ``/start`` answering a 302 to ``/final``, which answers 200, each once, so both must be sent."""
    httpx2_mock.add_response(
        status_code=httpx2.codes.FOUND,
        headers={"Location": "https://example.test/final"},
        url="https://example.test/start",
    )
    httpx2_mock.add_response(text="landed", url="https://example.test/final")


async def test_capped_response_reports_the_final_url(httpx2_mock: HTTPXMock) -> None:
    """Check that a redirected response reports the URL of the hop that answered."""
    client = _Client()
    _redirecting(httpx2_mock)
    resp = await client.get("start", expect=RESPONSE)
    assert resp.url.path == "/final", resp.url
    await client.aclose()


async def test_capped_response_keeps_the_redirect_history(httpx2_mock: HTTPXMock) -> None:
    """Check that the rebuilt response keeps its redirect chain."""
    client = _Client()
    _redirecting(httpx2_mock)
    resp = await client.get("start", expect=RESPONSE)
    assert [r.status_code for r in resp.history] == [httpx2.codes.FOUND]
    assert resp.history[0].url.path == "/start"
    await client.aclose()


async def test_no_redirect_still_reports_its_own_url(httpx2_mock: HTTPXMock) -> None:
    """Check that an unredirected response reports its own URL and an empty history."""
    client = _Client()
    httpx2_mock.add_response(text="ok")
    resp = await client.get("plain", expect=RESPONSE)
    assert resp.url.path == "/plain"
    assert resp.history == []
    await client.aclose()
