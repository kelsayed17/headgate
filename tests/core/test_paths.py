# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""Path fragments: whole segments in a row, anywhere in the path, as a rule, a route and the cache's filter match."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, cast, override

import anysqlite
import httpx2
import pytest
from hishel import AsyncSqliteStorage

from headgate import Client, HttpOptions, Route, TransportPolicy
from headgate._core import CallContext, check_fragment, path_has_fragment, url_path
from headgate.cache import HttpCache
from headgate.errors import UpstreamStatusError
from headgate.ratelimit import Rate
from headgate.retry import RetryPolicy
from tests.support.responses import RESPONSE

if TYPE_CHECKING:
    from collections.abc import Callable


@pytest.mark.parametrize(
    ("path", "fragment"),
    [
        ("/v2/exports", "/exports"),
        ("/v2/exports/orders.csv", "/exports"),
        ("/v2/exports/orders.csv", "exports/"),
        ("/v2/orders/archive/7", "orders/archive"),
        ("/exports", "/exports/"),
    ],
)
def test_a_fragment_matches_its_segments_anywhere_in_the_path(path: str, fragment: str) -> None:
    """Match the fragment's segments in a row, wherever they sit, whatever slashes surround the fragment."""
    assert path_has_fragment(path, fragment)


@pytest.mark.parametrize(
    ("path", "fragment"),
    [
        ("/v2/exports-archive", "/exports"),
        ("/v2/reexports", "/exports"),
        ("/v2/exports.csv", "/exports"),
        ("/v2/orders/7/archive", "orders/archive"),
        ("/v2", "/v2/orders"),
    ],
)
def test_a_fragment_does_not_match_part_of_a_segment(path: str, fragment: str) -> None:
    """Refuse a path holding the fragment only as part of a segment, or with its segments apart."""
    assert not path_has_fragment(path, fragment)


@pytest.mark.parametrize(
    ("url", "fragment", "held"),
    [
        ("https://x/v2/orders/archive/7?page=2", "orders/archive", True),
        ("https://x/files/exports%2Fq3", "/exports", False),
        ("https://x/files/exports%2Fq3", "exports%2Fq3", True),
        ("https://x/caf%C3%A9/menu", "café", True),
        ("https://x/v2/exports-archive", "/exports", False),
        ("https://x", "/v2", False),
    ],
    ids=["segments", "escaped slash", "escaped fragment", "decoded", "part of a segment", "no path"],
)
def test_a_urls_segments_are_split_before_they_are_decoded(url: str, fragment: str, *, held: bool) -> None:
    """Split a URL's raw path, then decode each segment: an escaped ``/`` stays inside the segment it was in."""
    assert path_has_fragment(url_path(httpx2.URL(url)), fragment) is held


@pytest.mark.parametrize(
    "build",
    [
        lambda: check_fragment("//"),
        lambda: Route("", rate=Rate.per_second(1)),
        lambda: Route("/"),
        lambda: HttpCache(storage=cast("Any", None), paths=("/orders", "")),
    ],
    ids=["check", "rate-rule", "route", "cache-filter"],
)
def test_a_fragment_that_names_no_segment_is_refused(build: Callable[[], object]) -> None:
    """Refuse a fragment that would match every path, wherever a fragment is taken."""
    with pytest.raises(ValueError, match="fragment must name at least one path segment"):
        build()


async def test_an_escaped_slash_in_a_value_does_not_pick_another_paths_route() -> None:
    """Leave ``t"/files/{name}"`` with ``name="exports/q3"`` to the client's retries, not the ``/exports`` route's."""
    wire = _Counting(503)
    client = Client(
        base_url="https://x.example",
        rate=Rate.per_second(1000),
        policy=TransportPolicy(retry=RetryPolicy(attempts=2), routes=(Route("/exports", retry=None),)),
        http=HttpOptions(transport=wire),
    )
    name = "exports/q3"
    with pytest.raises(UpstreamStatusError):
        await client.get(t"/files/{name}", expect=RESPONSE)
    with pytest.raises(UpstreamStatusError):
        await client.get(t"/exports/{name}", expect=RESPONSE)
    assert wire.calls == [2, 1], "the escaped value is retried by the client's policy; the real route is not"


@pytest.mark.parametrize(
    ("rule", "cached"),
    [("/exports", False), (re.compile(r"exports/q3"), True)],
    ids=["fragment", "pattern on the decoded path"],
)
async def test_the_cache_filter_matches_fragments_by_segment_and_patterns_on_the_decoded_path(
    rule: str | re.Pattern[str], *, cached: bool
) -> None:
    """Keep an escaped ``/`` inside its segment for a fragment; let a pattern see the path decoded, as it is written."""
    wire = _Counting(200)
    storage = AsyncSqliteStorage(connection=await anysqlite.connect(":memory:"))
    client = Client(
        base_url="https://x.example",
        rate=Rate.per_second(1000),
        policy=TransportPolicy(cache=HttpCache(storage=storage, paths=(rule,), ttl=60)),
        http=HttpOptions(transport=wire),
    )
    name = "exports/q3"
    try:
        for _ in range(2):
            await client.get(t"/files/{name}", expect=RESPONSE)
    finally:
        await storage.close()
    assert sum(wire.calls) == (1 if cached else 2)


class _Counting(httpx2.AsyncBaseTransport):
    """Answer *status* with an empty JSON object, counting the sends of each call in ``calls``."""

    def __init__(self, status: int) -> None:
        """Answer *status*."""
        self._status = status
        self.calls: list[int] = []
        self._last: object = None

    @override
    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        context = CallContext.of(request)
        if context is not self._last:
            self._last = context
            self.calls.append(0)
        self.calls[-1] += 1
        return httpx2.Response(self._status, content=b"{}", headers={"Content-Type": "application/json"})
