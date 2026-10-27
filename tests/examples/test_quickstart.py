# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""``examples/quickstart.py`` and ``examples/boundary.py``, the README's samples, run against a fake network.

The first two tests are the README's "Testing your client" sample, verbatim. The module runs with the real rate-budget
wait (``transport_policy``), as a client's own suite would.
"""

from __future__ import annotations

import functools
from http import HTTPStatus

import boundary
import httpx2
import pytest
import quickstart
from quickstart import CatalogClient

from headgate import (
    AuthError,
    CircuitOpenError,
    ClientConfigurationError,
    ClientError,
    ClientTimeoutError,
    RateLimitError,
    ResponseParseError,
    UpstreamStatusError,
)
from headgate.testing import FakeTransport, Respond, TestClock, isolated_breakers

pytestmark = pytest.mark.transport_policy

GDP = {"id": "GDP", "title": "Gross domestic product", "values": [27.4, 28.1]}


async def test_find_series_reads_a_404_as_none() -> None:
    """Return ``None`` for the 404 the endpoint documents, signing the request with the key."""
    fake = FakeTransport().reply("GET", "/v1/series/RETIRED", 404)
    async with CatalogClient("test-key", transport=fake) as catalog:
        assert await catalog.find_series("RETIRED") is None
    assert fake.calls[0].headers["Authorization"] == "Bearer test-key"


async def test_a_503_is_retried_after_the_wait_it_asks_for() -> None:
    """Retry a 503 once its ``Retry-After`` has passed, on a test clock that makes the wait instant."""
    fake = FakeTransport().reply("GET", "/v1/series/GDP", Respond(503, retry_after=2.0), Respond(json=GDP))
    clock = TestClock()
    with clock.install(), isolated_breakers():  # the clients built in the block use them
        catalog = CatalogClient("test-key", transport=fake)
    async with catalog:
        series = await catalog.get_series("GDP")
    assert series.values == [27.4, 28.1]
    assert clock.sleeps == [2.0]


async def test_main_reads_a_series_misses_one_and_follows_every_page(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run the README's usage sample: a series, a declared 404, and two pages of releases joined by a cursor."""
    fake = (
        FakeTransport()
        .reply("GET", "/v1/series/GDP", httpx2.Response(200, json=GDP))
        .reply("GET", "/v1/series/RETIRED", 404)
        .reply(
            "GET",
            "/v1/releases",
            httpx2.Response(200, json={"releases": [{"id": 1, "name": "GDP Q1"}], "next_cursor": "c2"}),
            httpx2.Response(200, json={"releases": [{"id": 2, "name": "GDP Q2"}]}),
        )
    )
    monkeypatch.setattr(quickstart, "CatalogClient", functools.partial(CatalogClient, transport=fake))

    latest, retired, names = await quickstart.main("test-key")

    assert (latest, retired, names) == (28.1, None, ["GDP Q1", "GDP Q2"])
    pages = fake.calls_to("GET", "/v1/releases")
    assert [page.url.params.get("cursor") for page in pages] == [None, "c2"]
    assert fake.closed


async def test_an_undeclared_404_raises_with_the_status_and_the_request_line() -> None:
    """Raise ``UpstreamStatusError`` for a 404 ``get_series`` did not declare, carrying the call's metadata."""
    fake = FakeTransport().reply("GET", "/v1/series/NOPE", httpx2.Response(404, headers={"x-request-id": "r-1"}))
    async with CatalogClient("test-key", transport=fake) as catalog:
        with pytest.raises(UpstreamStatusError) as caught:
            await catalog.get_series("NOPE")
    error = caught.value
    assert (error.status_code, error.method, error.url, error.request_id) == (404, "GET", "series/NOPE", "r-1")
    assert (error.attempts, error.retryable) == (1, False)
    assert str(error).startswith("[CatalogClient] ")


async def test_a_path_value_is_escaped_as_one_segment() -> None:
    """Escape a slash inside an id, so the value stays one path segment."""
    fake = FakeTransport().reply("GET", "/v1/series/A%2FB", httpx2.Response(200, json={**GDP, "id": "A/B"}))
    async with CatalogClient("test-key", transport=fake) as catalog:
        assert (await catalog.get_series("A/B")).id == "A/B"


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (ClientTimeoutError("slow"), HTTPStatus.GATEWAY_TIMEOUT),
        (ClientConfigurationError("rotating key"), HTTPStatus.SERVICE_UNAVAILABLE),
        (CircuitOpenError("open"), HTTPStatus.BAD_GATEWAY),
        (AuthError("refused", status_code=HTTPStatus.UNAUTHORIZED), HTTPStatus.BAD_GATEWAY),
        (RateLimitError("quota", status_code=HTTPStatus.TOO_MANY_REQUESTS), HTTPStatus.BAD_GATEWAY),
        (ResponseParseError("drift"), HTTPStatus.BAD_GATEWAY),
    ],
)
def test_gateway_status_maps_the_error_tree(error: ClientError, status: HTTPStatus) -> None:
    """Map a timeout to 504, our own misconfiguration to 503, and every other failure to 502."""
    assert boundary.gateway_status(error) is status
