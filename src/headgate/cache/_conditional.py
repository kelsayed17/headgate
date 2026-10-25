# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""Conditional reads: ask the origin whether what the caller already holds is still current.

The caller keeps the :class:`Validator` (``ETag`` and/or ``Last-Modified``) of the representation it holds and wraps
its reader with :func:`conditional`. The call then sends ``If-None-Match`` / ``If-Modified-Since`` (RFC 9110 §13.1) and
returns :class:`Fresh` with the new value and its validator, or :class:`NotModified` when the origin answers 304, in
which case the caller keeps what it has. Nothing is stored by headgate: the representation and its validator are the
caller's.

This is independent of :class:`~headgate.cache.HttpCache`. A ``FilterPolicy`` cache is TTL-only and never revalidates,
so a conditional call it can answer from storage comes back :class:`Fresh` with the stored body, without asking the
origin; only a miss reaches the origin with the validator.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import httpx2

from headgate._responses import Expect

if TYPE_CHECKING:
    from headgate._responses import Reader

__all__ = ["Fresh", "NotModified", "Validator", "conditional"]


@dataclass(frozen=True, slots=True, kw_only=True)
class Validator:
    """What identifies the representation a caller holds: its ``ETag`` and its ``Last-Modified``, each as sent."""

    etag: str | None = None
    last_modified: str | None = None

    @classmethod
    def of(cls, response: httpx2.Response, *, held: Validator | None = None) -> Validator:
        """Return *response*'s validator, each part it does not carry taken from *held*."""
        etag = response.headers.get("ETag")
        last_modified = response.headers.get("Last-Modified")
        if held is not None:
            etag = etag if etag is not None else held.etag
            last_modified = last_modified if last_modified is not None else held.last_modified
        return cls(etag=etag, last_modified=last_modified)

    def headers(self) -> dict[str, str]:
        """Return the precondition headers that ask whether this representation is still current."""
        headers: dict[str, str] = {}
        if self.etag is not None:
            headers["If-None-Match"] = self.etag
        if self.last_modified is not None:
            headers["If-Modified-Since"] = self.last_modified
        return headers


@dataclass(frozen=True, slots=True)
class Fresh[T]:
    """The origin sent a representation: its *value*, and the *validator* to send next time."""

    value: T
    validator: Validator


@dataclass(frozen=True, slots=True)
class NotModified:
    """The origin answered 304: what the caller holds is current. *validator* is the one to send next time."""

    validator: Validator


def conditional[T](reader: Reader[T], *, validator: Validator | None = None) -> Expect[Fresh[T] | NotModified]:
    """Wrap *reader* into a conditional read: send *validator*'s preconditions, and read a 2xx or a 304.

    Pass the result as ``expect=``. Without a *validator* (the first read) no precondition is sent and the result is
    always :class:`Fresh`, carrying the validator for the next call. A precondition the call sets itself through
    ``headers=`` wins over *validator*'s.
    """

    def fresh(response: httpx2.Response, /) -> Fresh[T]:
        return Fresh(reader(response), Validator.of(response))

    def not_modified(response: httpx2.Response, /) -> NotModified:
        return NotModified(Validator.of(response, held=validator))

    preconditions = {} if validator is None else validator.headers()
    return Expect[Fresh[T] | NotModified](fresh, headers=preconditions).on(httpx2.codes.NOT_MODIFIED, not_modified)
