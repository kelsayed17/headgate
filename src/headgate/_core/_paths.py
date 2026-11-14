# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""Path fragments: how a rule, a route and the cache's path filter name the requests they apply to.

A fragment is one or more whole path segments (``"/exports"``, ``"orders/archive"``), and it matches a URL path that
holds those segments in a row, anywhere in it. So ``"/exports"`` matches ``/v2/exports`` and ``/v2/exports/orders.csv``
but not ``/v2/exports-archive``, which a substring match would also catch. Leading and trailing slashes are ignored,
so a fragment does not depend on ``base_url``'s path.

A path is matched as it is sent, percent-encoded (:func:`url_path`): it is split into segments first and each is
decoded after, so a ``/`` a t-string value escaped as ``%2F`` stays inside its segment instead of making the path match
another fragment's rule.
"""

from __future__ import annotations

import functools
from typing import TYPE_CHECKING
from urllib.parse import unquote

if TYPE_CHECKING:
    import httpx2

__all__ = ["check_fragment", "path_has_fragment", "path_segments", "segments_hold", "url_path"]


@functools.lru_cache(maxsize=256)
def _segments(text: str) -> tuple[str, ...]:
    """Return the decoded segments of the fragment *text*; cached, since a fragment is matched against every request."""
    return path_segments(text)


def url_path(url: httpx2.URL) -> str:
    """Return *url*'s path as it is sent, percent-encoded, without the query: what a fragment is matched against."""
    path, _, _ = url.raw_path.partition(b"?")
    return path.decode("ascii")


def path_segments(path: str) -> tuple[str, ...]:
    """Return the non-empty segments of the encoded *path*, each decoded after the split."""
    return tuple(unquote(segment) for segment in path.split("/") if segment)


def segments_hold(found: tuple[str, ...], fragment: str) -> bool:
    """Return whether the path segments *found* (:func:`path_segments`) hold *fragment*'s segments in a row."""
    wanted = _segments(fragment)
    width = len(wanted)
    return any(found[start : start + width] == wanted for start in range(len(found) - width + 1))


def check_fragment(fragment: str) -> None:
    """Refuse a *fragment* that names no segment, such as ``""`` or ``"/"``, which would match every path."""
    if not _segments(fragment):
        msg = f"fragment must name at least one path segment (got {fragment!r})"
        raise ValueError(msg)


def path_has_fragment(path: str, fragment: str) -> bool:
    """Return whether the encoded URL path *path* (:func:`url_path`) holds *fragment*'s segments in a row."""
    return segments_hold(path_segments(path), fragment)
