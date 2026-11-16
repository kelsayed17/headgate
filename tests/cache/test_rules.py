# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""What an :class:`~headgate.HttpCache` keeps: :meth:`caches_request` and :meth:`stores_status`.

Each rule reads only the method, the URL path or the status, never the body or a credential.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import pytest

from headgate import HttpCache

if TYPE_CHECKING:
    from collections.abc import Callable

    from hishel import AsyncSqliteStorage


def _keeps(cache: HttpCache, method: str, path: str, status: int) -> bool:
    """Return whether *cache* stores the answer *status* to *method* on *path*."""
    return cache.caches_request(method, path) and cache.stores_status(status)


async def test_a_cache_stores_the_200s_of_every_get_by_default(storage: AsyncSqliteStorage) -> None:
    """Store a GET's 200 on any path, and neither a HEAD, a POST nor a 404, when the cache names nothing."""
    cache = HttpCache(storage=storage)
    assert _keeps(cache, "GET", "/any/path", 200)
    assert _keeps(cache, "get", "/any/path", 200), "a method in any case"
    assert not _keeps(cache, "HEAD", "/any/path", 200)
    assert not _keeps(cache, "POST", "/any/path", 200)
    assert not _keeps(cache, "GET", "/any/path", 404)


async def test_a_cache_stores_what_its_methods_paths_and_statuses_name(storage: AsyncSqliteStorage) -> None:
    """Store only the methods, path fragments and statuses the cache names, whichever iterables they come in."""
    cache = HttpCache(
        storage=storage, methods={"get", "HEAD"}, paths=["series/observations", "/releases"], statuses=[200, 203]
    )
    assert _keeps(cache, "HEAD", "/v1/series/observations", 203)
    assert _keeps(cache, "GET", "/v1/releases/dates", 200)
    assert not _keeps(cache, "GET", "/v1/series", 200), "a fragment matches whole segments, all of them"
    assert not _keeps(cache, "GET", "/v1/releasesx", 200)
    assert not _keeps(cache, "GET", "/v1/releases", 204)
    assert (cache.methods, cache.paths, cache.statuses) == (
        frozenset({"GET", "HEAD"}),
        ("series/observations", "/releases"),
        {200, 203},
    )


async def test_a_cache_stores_the_paths_its_patterns_match(storage: AsyncSqliteStorage) -> None:
    """Store a path a pattern matches, even inside a segment, beside the whole-segment fragments; keep it as given."""
    archive = re.compile(r"/fut_fin_txt_\d{4}\.zip$")
    cache = HttpCache(storage=storage, paths=[archive, "series"])
    assert cache.caches_request("GET", "/history/fut_fin_txt_2019.zip")
    assert cache.caches_request("GET", "/v1/series/GDP")
    assert not cache.caches_request("GET", "/history/fut_fin_txt_2019.txt")
    assert cache.paths == (archive, "series")


async def test_empty_methods_paths_or_statuses_store_nothing(storage: AsyncSqliteStorage) -> None:
    """Store nothing for an empty ``methods``, ``paths`` or ``statuses``: empty is not the default."""
    for cache in (
        HttpCache(storage=storage, methods=()),
        HttpCache(storage=storage, statuses=()),
        HttpCache(storage=storage, paths=()),
    ):
        assert not _keeps(cache, "GET", "/any/path", 200)


@pytest.mark.parametrize(
    ("build", "message"),
    [
        (lambda s: HttpCache(storage=s, paths=["/"]), "fragment must name at least one path segment"),
        (lambda s: HttpCache(storage=s, paths=[re.compile(r"\d*")]), r"must not match an empty path.*'\\\\d\*'"),
        (
            lambda s: HttpCache(storage=s, methods={"GET", "put", "post"}),
            r"^methods= may name only GET and HEAD \(got POST, PUT\): the cache keys an entry by its URL",
        ),
        (
            lambda s: HttpCache(storage=s, statuses={200, 206, 404}),
            r"^statuses= may name only 2xx statuses other than 206 \(got 206, 404\): hishel stores no other",
        ),
    ],
    ids=["empty-path", "everything-pattern", "body-method", "unstorable-status"],
)
async def test_a_cache_it_could_not_keep_is_refused(
    storage: AsyncSqliteStorage, build: Callable[[AsyncSqliteStorage], HttpCache], message: str
) -> None:
    """Refuse a path rule that matches every path, a method with a body, and a status hishel never stores."""
    with pytest.raises(ValueError, match=message):
        build(storage)
