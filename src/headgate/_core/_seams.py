# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""Seams two policy areas meet at, defined once so neither has to import the other.

* :class:`CredentialStamper`: what an auth declares about the credentials it puts on a request. The credential guard
  uses it to keep them on trusted origins; the cache uses it to partition entries by credential and to keep
  credentials out of storage. Its first two members, :class:`CredentialCarrier`, are what every middleware declares.
* :data:`EXPLICIT_HEADERS`: set by the client on every request whose call or view sets ``headers=``. The credential
  guard keeps those headers off untrusted origins; the cache partitions entries by the view's.
* :data:`BREAKER_GATE`: set by the client on every call, read by the retry layer, so a retry sequence stops as soon as
  the origin's breaker opens without the retry layer knowing about breakers.
* :class:`BudgetBacklogError` and :class:`BudgetPausedError`: raised by the rate budget for an attempt whose turn, or
  the server's pause, ends after the call's deadline. The retry layer withdraws the attempt, which was never sent, and
  keeps the call's last failure when it has one; the client raises them as ``ClientTimeoutError`` and
  ``RateLimitError``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Protocol, runtime_checkable

from headgate._core._context import Slot

if TYPE_CHECKING:
    import httpx2

__all__ = [
    "BREAKER_GATE",
    "EXPLICIT_HEADERS",
    "BudgetBacklogError",
    "BudgetPausedError",
    "CredentialCarrier",
    "CredentialStamper",
    "ExplicitHeaders",
]


@runtime_checkable
class CredentialCarrier(Protocol):
    """Something that declares the credentials it puts on a request: an auth, or a middleware."""

    @property
    def credential_headers(self) -> frozenset[str]:
        """Return the names of the headers it sets (compared case-insensitively)."""
        ...

    @property
    def credential_params(self) -> frozenset[str]:
        """Return the names of the query parameters it sets."""
        ...


@runtime_checkable
class CredentialStamper(CredentialCarrier, Protocol):
    """An auth that declares the credentials it stamps on a request, and names the credential they stand for."""

    def credential_identity(self) -> bytes:
        """Return stable bytes identifying the credential (its secret material); never stored, only hashed."""
        ...


#: The request extension holding a request's :class:`ExplicitHeaders`. httpx2 hands a request's extensions to the
#: redirects that follow it.
EXPLICIT_HEADERS: Final = "headgate.explicit_headers"


@dataclass(frozen=True, slots=True)
class ExplicitHeaders:
    """The headers a call set beyond the client's: its view's (``with_options(headers=...)``) and its own.

    Names are lower case. *origin* is the origin the code addressed: the call's URL, or for a later page of a walk, the
    first page's, since the server chose the later page's URL.
    """

    origin: httpx2.Origin
    view: frozenset[str]
    call: frozenset[str]

    def withheld_at(self, origin: httpx2.Origin) -> frozenset[str]:
        """Return the names to withhold from untrusted *origin*: the view's always, the call's off the addressed one.

        A view's headers are defaults like ``HttpOptions.headers``; a call's are meant for the URL the call named, so
        they still reach it when it is another origin, but no redirect or next page taking them elsewhere.
        """
        return self.view if origin == self.origin else self.view | self.call


#: Answers whether the breaker for the call's origin is refusing right now. The client puts it on every call's
#: context; the retry layer checks it before scheduling another attempt.
BREAKER_GATE = Slot[Callable[[], bool]]("breaker_gate")


class BudgetBacklogError(Exception):
    """Raised by ``Pacer.wait`` when an attempt's paced turn comes after its call's deadline, so it is never sent.

    ``retry_after`` is the seconds until the turn the attempt would have had. It never reaches a caller: the client
    raises it as a ``ClientTimeoutError`` carrying that ``retry_after``.
    """

    def __init__(self, message: str, *, retry_after: float) -> None:
        """Record *message* and *retry_after*."""
        super().__init__(message)
        self.retry_after = retry_after


class BudgetPausedError(Exception):
    """Raised by ``Pacer.wait`` when the server's pause outlasts the call's deadline, so the attempt is never sent.

    ``AsyncBaseClient`` raises it to its caller as :class:`headgate.errors.RateLimitError` with no status code, since
    the call it refuses got no response. ``retry_after`` is the seconds left in the pause.
    """

    def __init__(self, message: str, *, retry_after: float) -> None:
        """Record *message* and *retry_after*."""
        super().__init__(message)
        self.retry_after = retry_after
