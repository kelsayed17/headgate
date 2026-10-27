# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""Path fragments: how a rule, a route and the cache's path filter name the requests they apply to.

A fragment is one or more whole path segments (``"/exports"``, ``"orders/archive"``), and it matches a URL path that
holds those segments in a row, anywhere in it. So ``"/exports"`` matches ``/v2/exports`` and ``/v2/exports/orders.csv``
but not ``/v2/exports-archive``, which a substring match would also catch. Leading and trailing slashes are ignored,
so a fragment does not depend on ``base_url``'s path.
"""

from __future__ import annotations

import functools

__all__ = ["check_fragment", "path_has_fragment"]


@functools.lru_cache(maxsize=256)
def _segments(text: str) -> tuple[str, ...]:
    """Return the non-empty segments of the path *text*; cached, since a fragment is matched against every request."""
    return tuple(segment for segment in text.split("/") if segment)


def check_fragment(fragment: str) -> None:
    """Refuse a *fragment* that names no segment, such as ``""`` or ``"/"``, which would match every path."""
    if not _segments(fragment):
        msg = f"fragment must name at least one path segment (got {fragment!r})"
        raise ValueError(msg)


def path_has_fragment(path: str, fragment: str) -> bool:
    """Return whether the URL path *path* holds *fragment*'s segments in a row."""
    wanted = _segments(fragment)
    found = tuple(segment for segment in path.split("/") if segment)
    width = len(wanted)
    return any(found[start : start + width] == wanted for start in range(len(found) - width + 1))
