# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""What an :class:`~headgate.HttpCache` keeps, as the stack's ``caches_request`` and ``stores_status`` decide it.

Each rule reads only the method, the URL path or the status, never the body or a credential.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import TYPE_CHECKING, Any, cast

import pytest

from headgate import HttpCache
from headgate.cache import Partition
from headgate.cache._policy import caches_request, stores_status

if TYPE_CHECKING:
    from collections.abc import Callable

    from hishel import AsyncSqliteStorage


def _keeps(cache: HttpCache, method: str, path: str, status: int) -> bool:
    """Return whether *cache* stores the answer *status* to *method* on *path*."""
    return caches_request(cache, method, path) and stores_status(cache, status)


def test_a_cache_is_read_by_its_fields(storage: AsyncSqliteStorage) -> None:
    """Show what a cache keeps in ``methods``, ``paths`` and ``statuses``: the predicates over them are the stack's."""
    cache = HttpCache(storage=storage, paths=["series"])

    assert not hasattr(HttpCache, "caches_request")
    assert not hasattr(HttpCache, "stores_status")
    assert (cache.methods, cache.paths, cache.statuses) == (frozenset({"GET"}), ("series",), frozenset({200}))


async def test_a_cache_stores_the_200s_of_every_get_by_default(storage: AsyncSqliteStorage) -> None:
    """Store a GET's 200 on any path, and neither a HEAD, a POST nor a 404, when the cache names nothing."""
    cache = HttpCache(storage=storage)
    assert _keeps(cache, "GET", "/any/path", 200)
    assert _keeps(cache, "get", "/any/path", 200), "a method in any case"
    assert not _keeps(cache, "HEAD", "/any/path", 200)
    assert not _keeps(cache, "POST", "/any/path", 200)
    assert not _keeps(cache, "GET", "/any/path", 404)


async def test_the_cache_defaults_are_values(storage: AsyncSqliteStorage) -> None:
    """Default to the values themselves, GET and 200, so ``None`` keeps meaning off and is refused here."""
    cache = HttpCache(storage=storage)
    assert (cache.methods, cache.paths, cache.statuses) == (frozenset({"GET"}), None, frozenset({200}))
    for option in ("methods", "statuses"):
        with pytest.raises(TypeError, match=rf"^{option}= takes "):
            HttpCache(storage=storage, **cast("Any", {option: None}))


def test_partition_is_a_str_enum() -> None:
    """Make ``Partition`` a ``StrEnum``, as ``BreakerState`` and ``CacheOutcome`` are."""
    assert issubclass(Partition, StrEnum)
    assert (str(Partition.CREDENTIAL), str(Partition.SHARED)) == ("credential", "shared")


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
    assert caches_request(cache, "GET", "/history/fut_fin_txt_2019.zip")
    assert caches_request(cache, "GET", "/v1/series/GDP")
    assert not caches_request(cache, "GET", "/history/fut_fin_txt_2019.txt")
    assert cache.paths == (archive, "series")


@pytest.mark.parametrize("option", ["methods", "paths", "statuses"])
async def test_an_empty_rule_is_refused(storage: AsyncSqliteStorage, option: str) -> None:
    """Refuse an empty ``methods``, ``paths`` or ``statuses``, which would store nothing: ``cache=None`` is no cache."""
    message = rf"^{option}= is empty, so the cache would store nothing: write cache=None for a client without a cache$"
    with pytest.raises(ValueError, match=message):
        HttpCache(storage=storage, **cast("Any", {option: ()}))


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
