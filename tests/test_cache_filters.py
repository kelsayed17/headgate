"""Native hishel ``BaseFilter`` subclasses (no I/O).

These three composable filters are the native-hishel primitives each cached client composes into its own
:class:`hishel.FilterPolicy` (in ``headgate._cache``, and in each provider's ``cache.py``).
Each is a plain :class:`hishel.BaseFilter` — ``needs_body()`` is always ``False``
(they inspect only the request line / status code, never the body) and
``apply(item, body)`` returns whether the item should be cached.

hishel's own :class:`hishel.Request` / :class:`hishel.Response` are constructed
directly (positional ``method, url`` for the request; keyword ``status_code``
for the response), matching the shape hishel hands each filter at runtime.
"""

from __future__ import annotations

from hishel import Request, Response

from headgate._cache import (
    MethodFilter,
    PathFragmentFilter,
    StatusCodeFilter,
)


def test_path_fragment_filter_matches_any_fragment() -> None:
    f = PathFragmentFilter(("series/observations", "releases/dates"))

    assert f.needs_body() is False

    # A request whose URL path contains the first fragment -> True.
    obs = Request("GET", "https://api.stlouisfed.org/fred/series/observations?series_id=GDP")
    assert f.apply(obs, None) is True

    # A request whose URL path contains the second fragment -> True.
    dates = Request("GET", "https://api.stlouisfed.org/fred/releases/dates")
    assert f.apply(dates, None) is True

    # A request matching no fragment -> False.
    quote = Request("GET", "https://api.example.com/quotes/AAPL")
    assert f.apply(quote, None) is False


def test_method_filter() -> None:
    f = MethodFilter(frozenset({"POST"}))

    assert f.needs_body() is False

    post = Request("POST", "https://api.bls.gov/publicAPI/v2/timeseries/data/")
    assert f.apply(post, None) is True

    get = Request("GET", "https://api.bls.gov/publicAPI/v2/timeseries/data/")
    assert f.apply(get, None) is False


def test_status_code_filter_only_200() -> None:
    f = StatusCodeFilter(frozenset({200}))

    assert f.needs_body() is False

    assert f.apply(Response(status_code=200), None) is True
    assert f.apply(Response(status_code=404), None) is False
    assert f.apply(Response(status_code=500), None) is False
