# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""The cache's ``hishel.BaseFilter`` subclasses, which compose into a client's :class:`hishel.FilterPolicy`.

Each inspects only the request line or the status code, never the body, so ``needs_body()`` is always ``False`` and
``apply(item, body)`` says whether to cache. hishel's own :class:`hishel.Request` and :class:`hishel.Response` are built
here in the shape hishel hands a filter at runtime.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx2
import pytest
from hishel import FilterPolicy, Request, Response

from headgate.cache import HttpCache, MethodFilter, PathFragmentFilter, StatusCodeFilter
from headgate.cache._policy import filter_policy_of

if TYPE_CHECKING:
    from collections.abc import Callable

    from hishel import AsyncSqliteStorage


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


def test_each_filter_takes_any_iterable_and_any_method_case() -> None:
    """Accept a list or a set where a filter takes a collection, and a method in any case."""
    assert MethodFilter(["get"]).apply(Request("GET", "https://api.example.test/x"), None) is True
    assert PathFragmentFilter(["/x"]).apply(Request("GET", "https://api.example.test/x"), None) is True
    assert StatusCodeFilter([200]).apply(Response(status_code=httpx2.codes.OK), None) is True


def _stores(policy: FilterPolicy, method: str, url: str, status: int) -> bool:
    """Return whether *policy* stores the answer *status* to *method* *url*."""
    request, response = Request(method, url), Response(status_code=status)
    return all(f.apply(request, None) for f in policy.request_filters) and all(
        f.apply(response, None) for f in policy.response_filters
    )


async def test_a_cache_stores_the_200s_of_every_get_by_default(storage: AsyncSqliteStorage) -> None:
    """Store a GET's 200 on any path, and neither a POST nor a 404, when the cache names nothing."""
    policy = filter_policy_of(HttpCache(storage=storage))
    assert _stores(policy, "GET", "https://api.example.test/any/path", 200)
    assert not _stores(policy, "POST", "https://api.example.test/any/path", 200)
    assert not _stores(policy, "GET", "https://api.example.test/any/path", 404)


async def test_a_cache_stores_what_its_methods_paths_and_statuses_name(storage: AsyncSqliteStorage) -> None:
    """Store only the methods, path fragments and statuses the cache names."""
    cache = HttpCache(storage=storage, methods={"get", "HEAD"}, paths=["/series"], statuses=[200, 203])
    policy = filter_policy_of(cache)
    assert _stores(policy, "HEAD", "https://api.example.test/v1/series/GDP", 203)
    assert not _stores(policy, "GET", "https://api.example.test/v1/releases", 200)
    assert (cache.methods, cache.paths, cache.statuses) == (frozenset({"GET", "HEAD"}), ("/series",), {200, 203})


async def test_empty_methods_or_statuses_store_nothing(storage: AsyncSqliteStorage) -> None:
    """Store nothing for ``methods=()`` or ``statuses=()``, as ``paths=()`` does: empty is not the default."""
    for cache in (
        HttpCache(storage=storage, methods=()),
        HttpCache(storage=storage, statuses=()),
        HttpCache(storage=storage, paths=()),
    ):
        assert not _stores(filter_policy_of(cache), "GET", "https://api.example.test/any/path", 200)


async def test_a_policy_of_your_own_is_used_as_it_is(storage: AsyncSqliteStorage) -> None:
    """Store by the hishel policy given, in place of the three fields."""
    own = FilterPolicy(request_filters=[MethodFilter({"POST"})])
    assert filter_policy_of(HttpCache(storage=storage, policy=own)) is own


@pytest.mark.parametrize(
    ("build", "message"),
    [
        (
            lambda s: HttpCache(storage=s, policy=FilterPolicy(), methods={"GET"}),
            r"^policy= decides what is stored, so it cannot be combined with methods=, paths= or statuses=$",
        ),
        (lambda s: HttpCache(storage=s, paths=["/"]), "fragment must name at least one path segment"),
        (
            lambda s: HttpCache(storage=s, methods={"GET", "post"}),
            r"^methods= may name only GET and HEAD \(got POST\): .*FilterPolicy\(use_body_key=True",
        ),
    ],
    ids=["policy-and-methods", "empty-path", "body-method"],
)
async def test_a_contradictory_cache_is_refused(
    storage: AsyncSqliteStorage, build: Callable[[AsyncSqliteStorage], HttpCache], message: str
) -> None:
    """Refuse a policy beside the fields it replaces, a path that names no segment, and a method with a body."""
    with pytest.raises(ValueError, match=message):
        build(storage)
