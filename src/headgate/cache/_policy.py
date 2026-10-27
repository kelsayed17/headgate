# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""What a client's cache keeps, where, for how long, and whose it is: :class:`HttpCache` and its filters.

hishel ships no concrete filters; :class:`hishel.BaseFilter` is its extension point. :class:`PathFragmentFilter`,
:class:`MethodFilter` and :class:`StatusCodeFilter` compose into a :class:`hishel.FilterPolicy`, which **stores whenever
a request filter matches and an allowed status comes back, even past an RFC 9111** ``no-store``. That is the point for
a provider that sends ``no-store`` or no cache headers at all, where hishel's RFC 9111 policy would cache nothing. Store
only the 200s (a :class:`StatusCodeFilter`) so an error body never poisons the cache. Filters run inside hishel and see
the URL with its credentials removed (see :mod:`headgate.cache._credentials`), so no other layer evaluates a rule.
Body-keyed POST caching is hishel's own ``FilterPolicy.use_body_key``.

A ``FilterPolicy`` cache is TTL-only: an entry is served until its lifetime ends and is never revalidated with the
origin. A caller that wants revalidation sends its own validator with :func:`headgate.cache.conditional`.
"""

from __future__ import annotations

import enum
import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, override

import httpx2
from hishel import BaseFilter, Request, Response

from headgate._core import check_fragment, path_has_fragment

if TYPE_CHECKING:
    from hishel import AsyncBaseStorage, FilterPolicy

__all__ = ["HttpCache", "MethodFilter", "Partition", "PathFragmentFilter", "StatusCodeFilter"]


class Partition(enum.Enum):
    """Whose entries a cached response is visible to."""

    CREDENTIAL = "credential"
    """Only to requests made with the same credential: the default, and the only safe choice for private data.

    The partition is a keyed hash of the auth's :meth:`~headgate._core.CredentialStamper.credential_identity` and of
    any other credential the request carries, so two clients with different credentials never see each other's entries,
    even on one storage. It requires an auth that declares its credentials (a
    :class:`~headgate._core.CredentialStamper`), or no auth.
    """

    SHARED = "shared"
    """To every client on the storage, whatever its credential: for public data only.

    Credentials are still never stored. This is the explicit opt-in for an auth that does not declare its credentials.
    """


@dataclass(frozen=True, slots=True, kw_only=True)
class HttpCache:
    """A client's opt-in HTTP response cache: what to keep, where, for how long, and whose it is.

    ``policy`` decides what is stored. ``storage`` is the hishel store, which many clients may share and whoever
    created it closes. ``ttl`` is the client's default lifetime in seconds, which a per-call ``cache_ttl=`` overrides.
    ``partition`` decides who sees an entry (:class:`Partition`); entries are per credential unless it says otherwise.

    :raises ValueError: for a ``ttl`` that is not a positive finite number.
    """

    policy: FilterPolicy
    storage: AsyncBaseStorage
    ttl: float | None = None
    partition: Partition = Partition.CREDENTIAL

    def __post_init__(self) -> None:
        """Refuse a TTL no entry could live by."""
        if self.ttl is not None and not (self.ttl > 0 and math.isfinite(self.ttl)):
            msg = f"ttl must be a positive finite number of seconds, or None (got {self.ttl})"
            raise ValueError(msg)


# Filters. Each decides from the request line or the response status alone, so none needs the body.


class PathFragmentFilter(BaseFilter[Request]):
    """Cache a request whose URL path holds any of *fragments*, each one or more whole segments, as a rule matches."""

    def __init__(self, fragments: tuple[str, ...]) -> None:
        """Match a path holding any of *fragments*.

        :raises ValueError: for a fragment that names no path segment, which would match every path.
        """
        for fragment in fragments:
            check_fragment(fragment)
        self._fragments = fragments

    @override
    def needs_body(self) -> bool:
        """Return ``False``: the path is enough."""
        return False

    @override
    def apply(self, item: Request, body: bytes | None) -> bool:
        """Return whether *item*'s path holds one of the fragments."""
        path = httpx2.URL(item.url).path
        return any(path_has_fragment(path, fragment) for fragment in self._fragments)


class MethodFilter(BaseFilter[Request]):
    """Cache a request whose method is in *methods* (upper case, as httpx2 sends it)."""

    def __init__(self, methods: frozenset[str]) -> None:
        """Match a request whose method is one of *methods*."""
        self._methods = methods

    @override
    def needs_body(self) -> bool:
        """Return ``False``: the method is enough."""
        return False

    @override
    def apply(self, item: Request, body: bytes | None) -> bool:
        """Return whether *item*'s method is allowed."""
        return item.method in self._methods


class StatusCodeFilter(BaseFilter[Response]):
    """Store a response whose status is in *codes*, so an error body never poisons the cache."""

    def __init__(self, codes: frozenset[int]) -> None:
        """Match a response whose status is one of *codes*."""
        self._codes = codes

    @override
    def needs_body(self) -> bool:
        """Return ``False``: the status is enough."""
        return False

    @override
    def apply(self, item: Response, body: bytes | None) -> bool:
        """Return whether *item*'s status is allowed."""
        return item.status_code in self._codes
