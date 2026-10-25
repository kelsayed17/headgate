# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""The cache's ``hishel.BaseFilter`` subclasses, which compose into a client's :class:`hishel.FilterPolicy`.

Each inspects only the request line or the status code, never the body, so ``needs_body()`` is always ``False`` and
``apply(item, body)`` says whether to cache. hishel's own :class:`hishel.Request` and :class:`hishel.Response` are built
here in the shape hishel hands a filter at runtime.
"""

from __future__ import annotations

import httpx2
from hishel import Request, Response

from headgate.cache import MethodFilter, PathFragmentFilter, StatusCodeFilter


def test_path_fragment_filter_matches_any_fragment() -> None:
    """A request whose path contains any one of the fragments is cached, and one containing none is not."""
    f = PathFragmentFilter(("series/observations", "releases/dates"))

    assert f.needs_body() is False

    obs = Request("GET", "https://api.example.test/v1/series/observations?series_id=GDP")
    assert f.apply(obs, None) is True

    dates = Request("GET", "https://api.example.test/v1/releases/dates")
    assert f.apply(dates, None) is True

    quote = Request("GET", "https://api.example.test/quotes/AAPL")
    assert f.apply(quote, None) is False


def test_method_filter_matches_only_the_allowed_methods() -> None:
    """A request is cached only when its method is in the allowed set."""
    f = MethodFilter(frozenset({"POST"}))

    assert f.needs_body() is False

    post = Request("POST", "https://api.example.test/v2/timeseries/data/")
    assert f.apply(post, None) is True

    get = Request("GET", "https://api.example.test/v2/timeseries/data/")
    assert f.apply(get, None) is False


def test_status_code_filter_stores_only_the_allowed_statuses() -> None:
    """Only a response whose status is allowed is stored, so an error body never reaches the cache."""
    f = StatusCodeFilter(frozenset({httpx2.codes.OK}))

    assert f.needs_body() is False

    assert f.apply(Response(status_code=httpx2.codes.OK), None) is True
    assert f.apply(Response(status_code=httpx2.codes.NOT_FOUND), None) is False
    assert f.apply(Response(status_code=httpx2.codes.INTERNAL_SERVER_ERROR), None) is False
