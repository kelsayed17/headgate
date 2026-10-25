# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""Cache policies shared by the cache suite."""

from __future__ import annotations

from hishel import FilterPolicy

from headgate._cache import MethodFilter, PathFragmentFilter, StatusCodeFilter


def series_policy() -> FilterPolicy:
    """Cache the 200s of any path containing ``/series``."""
    return FilterPolicy(
        request_filters=[PathFragmentFilter(("/series",))],
        response_filters=[StatusCodeFilter(frozenset({200}))],
    )


def every_get_policy() -> FilterPolicy:
    """Cache the 200s of every GET, so hishel reaches the storage on each one."""
    return FilterPolicy(
        request_filters=[MethodFilter(frozenset({"GET"}))],
        response_filters=[StatusCodeFilter(frozenset({200}))],
    )
