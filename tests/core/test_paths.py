# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""Path fragments: whole segments in a row, anywhere in the path, as a rule, a route and the cache's filter match."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from headgate import RateRule, Route
from headgate._core import check_fragment, path_has_fragment
from headgate.cache import PathFragmentFilter
from headgate.ratelimit import Rate

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
    "build",
    [
        lambda: check_fragment("//"),
        lambda: RateRule("", Rate.per_second(1)),
        lambda: Route("/"),
        lambda: PathFragmentFilter(("/orders", "")),
    ],
    ids=["check", "rate-rule", "route", "cache-filter"],
)
def test_a_fragment_that_names_no_segment_is_refused(build: Callable[[], object]) -> None:
    """Refuse a fragment that would match every path, wherever a fragment is taken."""
    with pytest.raises(ValueError, match="fragment must name at least one path segment"):
        build()
