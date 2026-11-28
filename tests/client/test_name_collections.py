# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""A parameter that takes names or codes takes a collection of them, and refuses a bare ``str``.

A ``str`` is an ``Iterable[str]`` of its letters, so ``CassetteFilter(headers="x-api-key")`` scrubbed eleven letters and
recorded the key, and ``RetryPolicy(methods="GET")`` retried no GET. These parameters are typed ``Collection[...]`` and
refuse a bare ``str``, a number and a one-shot iterator, which reading to check would empty.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

import pytest

from headgate import HttpCache, RetryPolicy
from headgate.testing import CassetteFilter

if TYPE_CHECKING:
    from collections.abc import Callable

_STORAGE: Any = object()


@pytest.mark.parametrize(
    ("build", "message"),
    [
        (lambda: CassetteFilter(headers=cast("Any", "x-api-key")), r"^headers= takes a collection of header names"),
        (lambda: CassetteFilter(params=cast("Any", "api_key")), r"^params= takes a collection of query parameter"),
        (lambda: HttpCache(storage=_STORAGE, methods=cast("Any", "GET")), r"^methods= takes a collection of methods"),
        (lambda: HttpCache(storage=_STORAGE, paths=cast("Any", "/items")), r"^paths= takes a collection of path"),
        (lambda: HttpCache(storage=_STORAGE, statuses=cast("Any", 200)), r"^statuses= takes a collection of statuses"),
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


def test_http_cache_refuses_a_generator() -> None:
    """Refuse a one-shot iterator rather than consume it to check it."""
    with pytest.raises(TypeError, match=r"^methods= takes a collection"):
        HttpCache(storage=_STORAGE, methods=cast("Any", (method for method in ("GET",))))


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
