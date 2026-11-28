# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""A parameter that takes names or codes takes a collection of them, and refuses a bare ``str``.

A ``str`` is an ``Iterable[str]`` of its letters, so ``CassetteFilter(headers="x-api-key")`` scrubbed eleven letters and
recorded the key, and ``RetryPolicy(methods="GET")`` retried no GET. These parameters are typed ``Collection[...]`` and
refuse a bare ``str``, a number and a one-shot iterator, which reading to check would empty.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Final, cast

import pytest
from hishel import AsyncSqliteStorage

from headgate import HttpCache, HttpOptions, RetryPolicy, SecurityOptions
from headgate.testing import CassetteFilter

if TYPE_CHECKING:
    from collections.abc import Callable

_STORAGE: Final = AsyncSqliteStorage(database_path=":memory:")


@pytest.mark.parametrize(
    ("build", "message"),
    [
        (lambda: CassetteFilter(headers=cast("Any", "x-api-key")), r"^headers= takes a collection of header names"),
        (lambda: CassetteFilter(params=cast("Any", "api_key")), r"^params= takes a collection of query parameter"),
        (
            lambda: HttpCache(storage=_STORAGE, ttl=None, methods=cast("Any", "GET")),
            r"^methods= takes a collection of methods",
        ),
        (
            lambda: HttpCache(storage=_STORAGE, ttl=None, paths=cast("Any", "/items")),
            r"^paths= takes a collection of path",
        ),
        (
            lambda: HttpCache(storage=_STORAGE, ttl=None, statuses=cast("Any", 200)),
            r"^statuses= takes a collection of statuses",
        ),
        (lambda: RetryPolicy(methods=cast("Any", "GET")), r"^methods= takes a collection of methods"),
        (lambda: RetryPolicy(statuses=cast("Any", 503)), r"^statuses= takes a collection of statuses"),
    ],
    ids=[
        "cassette-headers",
        "cassette-params",
        "cache-methods",
        "cache-paths",
        "cache-statuses",
        "retry-methods",
        "retry-statuses",
    ],
)
def test_a_bare_value_is_refused_where_a_collection_goes(build: Callable[[], object], message: str) -> None:
    """Refuse a bare ``str`` or number, naming the option and showing a collection."""
    with pytest.raises(TypeError, match=message):
        build()


@pytest.mark.parametrize(
    ("build", "message"),
    [
        (
            lambda: HttpOptions(request_id_headers=cast("Any", ["x-a", 5])),
            r"^request_id_headers= takes a sequence of header names, in order, .* \(got int 5 among them\)$",
        ),
        (
            lambda: CassetteFilter(headers=cast("Any", ["x-key", b"x-other"])),
            r"^headers= takes a collection of header names, .* \(got bytes b'x-other' among them\)$",
        ),
        (
            lambda: RetryPolicy(methods=cast("Any", ["GET", None])),
            r"^methods= takes a collection of methods, .* \(got NoneType None among them\)$",
        ),
        (
            lambda: HttpCache(storage=_STORAGE, ttl=None, paths=cast("Any", ["/a", 1])),
            r"^paths= takes a collection of path fragments or patterns, .* \(got int 1 among them\)$",
        ),
        (
            lambda: SecurityOptions(trusted_origins=cast("Any", [5])),
            r"^trusted_origins= takes a collection of origins, .* \(got int 5 among them\)$",
        ),
    ],
    ids=["request-id-headers", "cassette-headers", "retry-methods", "cache-paths", "trusted-origins"],
)
def test_an_entry_of_the_wrong_kind_is_refused_naming_it(build: Callable[[], object], message: str) -> None:
    """Refuse an entry of the wrong kind where the collection is given, naming the entry rather than the collection."""
    with pytest.raises(TypeError, match=message):
        build()


def test_http_cache_refuses_a_pattern_of_bytes() -> None:
    """Refuse a pattern compiled from bytes, which the path, a ``str``, cannot be searched with."""
    with pytest.raises(TypeError, match=r"^paths= takes patterns of str, as a path is \(got the bytes pattern b'x'\)$"):
        HttpCache(storage=_STORAGE, ttl=None, paths=[cast("Any", re.compile(b"x"))])


def test_http_cache_refuses_a_generator() -> None:
    """Refuse a one-shot iterator rather than consume it to check it."""
    with pytest.raises(TypeError, match=r"^methods= takes a collection"):
        HttpCache(storage=_STORAGE, ttl=None, methods=cast("Any", (method for method in ("GET",))))


def test_cassette_filter_refuses_an_entry_that_declares_nothing() -> None:
    """Refuse an ``auths=`` entry that declares no credentials, which would contribute nothing to the filter."""
    with pytest.raises(TypeError, match=r"^auths= takes auths or middleware that declare credential_headers"):
        CassetteFilter(auths=cast("Any", ["token"]))


def test_cassette_filter_reads_a_middleware_declaration() -> None:
    """Filter what a middleware declares, as it filters what an auth declares."""

    class Signer:
        credential_headers = frozenset({"X-Signature"})
        credential_params = frozenset({"sig"})

    cassettes = CassetteFilter(auths=(Signer(),))

    assert "x-signature" in cassettes.filter_headers
    assert "sig" in cassettes.filter_query_parameters
