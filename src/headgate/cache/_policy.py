# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""What a client's cache keeps, where, for how long, and whose it is: :class:`HttpCache`.

hishel 2 caches every successful GET and HEAD it is handed, whatever the response's cache headers say. headgate
decides before and after it: a request whose method or path :class:`HttpCache` does not name never reaches hishel
(:meth:`HttpCache.caches_request`), and a response whose status it does not name is never stored
(:meth:`HttpCache.stores_status`). So a cache **stores whenever the request matches and an allowed status comes back,
even past an RFC 9111** ``no-store``. That is the point for a provider that sends ``no-store`` or no cache headers at
all, where an RFC 9111 cache would keep nothing. Store only the 200s (the default) so an error body never poisons the
cache. The rules read only the method and the URL path, never a credential. ``methods`` names only GET and HEAD,
which a URL keys: a POST is never cached.

The cache is TTL-only: an entry is served until its lifetime ends and is never revalidated with the origin. A caller
that wants revalidation sends its own validator with :func:`headgate.cache.conditional`.
"""

from __future__ import annotations

import enum
import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final
from urllib.parse import unquote

import httpx2

from headgate._core import check_fragment, path_has_fragment

if TYPE_CHECKING:
    import re
    from collections.abc import Iterable

    from hishel import AsyncBaseStorage

__all__ = ["HttpCache", "Partition"]

#: The methods ``HttpCache(methods=...)`` may name: those a URL identifies, since the URL is the cache key.
_URL_KEYED_METHODS: Final = frozenset({"GET", "HEAD"})

#: What ``methods`` and ``statuses`` mean when left ``None``.
_DEFAULT_METHODS: Final = frozenset({"GET"})
_DEFAULT_STATUSES: Final = frozenset({200})


class Partition(enum.Enum):
    """Whose entries a cached response is visible to."""

    CREDENTIAL = "credential"
    """Only to requests made with the same credential: the default, and the only safe choice for private data.

    The partition is a keyed hash of the auth's :meth:`~headgate.security.CredentialStamper.credential_identity` and of
    any other credential the request carries, so two clients with different credentials never see each other's entries,
    even on one storage. It requires an auth that declares its credentials (a
    :class:`~headgate.security.CredentialStamper`), or no auth.
    """

    SHARED = "shared"
    """To every client on the storage, whatever its credential: for public data only.

    Credentials are still never stored: what the auth and middleware declare is dropped, and every other request header
    is stored as a keyed digest, so an undeclared auth's header is not stored either. A query parameter with a secret
    name that the auth does not declare still keeps entries apart by its value, since it may be an ordinary parameter
    (``?key=theme``). This is the explicit opt-in for an auth that does not declare its credentials.
    """


@dataclass(frozen=True, slots=True, kw_only=True)
class HttpCache:
    """A client's opt-in HTTP response cache: what to keep, where, for how long, and whose it is.

    ``storage`` is the hishel store, which many clients may share and whoever created it closes. ``ttl`` is the
    client's default lifetime in seconds, which a per-call ``cache_ttl=`` overrides. ``partition`` decides who sees an
    entry (:class:`Partition`); entries are per credential unless it says otherwise.

    What is stored: a request whose method is in ``methods`` (``GET`` by default; ``GET`` and ``HEAD`` only, since the
    URL is the key) and whose path matches one of ``paths`` (any path by default), answered with a status in
    ``statuses`` (200 by default). A string in ``paths`` is one or more whole segments, matched as
    :class:`~headgate.Route` matches; a compiled pattern is searched for in the path, for what segments cannot say,
    such as a file name's prefix or extension. An empty collection matches nothing, so stores
    nothing. :meth:`caches_request` and :meth:`stores_status` say what the three decide.

    :raises ValueError: for a ``ttl`` that is not a positive finite number, a method in ``methods`` other than ``GET``
        and ``HEAD``, a status in ``statuses`` outside 2xx or a 206, or a fragment in ``paths`` that names no segment or
        a pattern that matches an empty path.
    """

    storage: AsyncBaseStorage
    ttl: float | None = None
    partition: Partition = Partition.CREDENTIAL
    methods: Iterable[str] | None = None
    paths: Iterable[str | re.Pattern[str]] | None = None
    statuses: Iterable[int] | None = None

    def __post_init__(self) -> None:
        """Refuse a TTL no entry could live by, a method a URL cannot key, and a path rule that matches every path.

        The collections are frozen.
        """
        if self.ttl is not None and not (self.ttl > 0 and math.isfinite(self.ttl)):
            msg = f"ttl must be a positive finite number of seconds, or None (got {self.ttl})"
            raise ValueError(msg)
        if self.methods is not None:
            methods = frozenset(method.upper() for method in self.methods)
            if bodied := sorted(methods - _URL_KEYED_METHODS):
                msg = (
                    f"methods= may name only GET and HEAD (got {', '.join(bodied)}): the cache keys an entry by its "
                    f"URL, so two requests with different bodies would share one"
                )
                raise ValueError(msg)
            object.__setattr__(self, "methods", methods)
        if self.paths is not None:
            paths = tuple(self.paths)
            for path in paths:
                _check_path(path)
            object.__setattr__(self, "paths", paths)
        if self.statuses is not None:
            statuses = frozenset(self.statuses)
            if unstorable := sorted(status for status in statuses if not _storable(status)):
                msg = (
                    f"statuses= may name only 2xx statuses other than 206 (got {', '.join(map(str, unstorable))}): "
                    f"hishel stores no other response, and a 206 holds only part of the body"
                )
                raise ValueError(msg)
            object.__setattr__(self, "statuses", statuses)

    def caches_request(self, method: str, path: str) -> bool:
        """Return whether a request with *method* (in any case) and the URL path *path* goes through the cache.

        *path* is the path as sent, percent-encoded (:func:`~headgate._core.url_path`): a fragment is matched segment by
        segment, so an escaped ``/`` stays in its segment, and a pattern is searched in the decoded path, as written.

        ``methods`` left ``None`` means ``GET``, and ``paths`` left ``None`` any path; an empty collection matches
        nothing. Any other request is sent past the cache: never looked up, never stored.
        """
        methods = _DEFAULT_METHODS if self.methods is None else self.methods
        if method.upper() not in methods:
            return False
        return self.paths is None or any(_matches(path, rule) for rule in self.paths)

    def stores_status(self, status: int) -> bool:
        """Return whether a response with *status* may be stored: one in ``statuses``, ``200`` when it is ``None``."""
        return status in (_DEFAULT_STATUSES if self.statuses is None else self.statuses)


def _storable(status: int) -> bool:
    """Return whether hishel stores a response with *status*: a 2xx, but not a 206, which holds part of a body."""
    return httpx2.codes.is_success(status) and status != httpx2.codes.PARTIAL_CONTENT


def _check_path(path: str | re.Pattern[str]) -> None:
    """Refuse a fragment naming no segment, and a pattern matching an empty path: each would match every path."""
    if isinstance(path, str):
        check_fragment(path)
    elif path.search("") is not None:
        msg = f"a path pattern must not match an empty path, or it matches every path (got {path.pattern!r})"
        raise ValueError(msg)


def _matches(path: str, rule: str | re.Pattern[str]) -> bool:
    """Return whether the URL path *path* holds the fragment *rule*, or contains a match of the pattern *rule*."""
    return path_has_fragment(path, rule) if isinstance(rule, str) else rule.search(unquote(path)) is not None
