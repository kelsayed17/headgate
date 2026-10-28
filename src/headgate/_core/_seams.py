# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""Seams two policy areas meet at, defined once so neither has to import the other.

* :class:`CredentialStamper`: what an auth declares about the credentials it puts on a request. The credential guard
  uses it to keep them on trusted origins; the cache uses it to partition entries by credential and to keep
  credentials out of storage. Its first two members, :class:`CredentialCarrier`, are what a signing middleware
  declares, for the guard alone.
* :data:`BREAKER_GATE`: set by the client on every call, read by the retry layer, so a retry sequence stops as soon as
  the origin's breaker opens without the retry layer knowing about breakers.
* :class:`BudgetBacklogError` and :class:`BudgetPausedError`: raised by the rate budget for an attempt whose turn, or
  the server's pause, ends after the call's deadline. The retry layer withdraws the attempt, which was never sent, and
  keeps the call's last failure when it has one; the client raises them as ``ClientTimeoutError`` and
  ``RateLimitError``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, runtime_checkable

from headgate._core._context import Slot

__all__ = ["BREAKER_GATE", "BudgetBacklogError", "BudgetPausedError", "CredentialCarrier", "CredentialStamper"]


@runtime_checkable
class CredentialCarrier(Protocol):
    """Something that declares the credentials it puts on a request: an auth, or a signing middleware."""

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


#: Answers whether the breaker for the call's origin is refusing right now. The client puts it on every call's
#: context; the retry layer checks it before scheduling another attempt.
BREAKER_GATE = Slot[Callable[[], bool]]("breaker_gate")


class BudgetBacklogError(Exception):
    """Raised by ``Pacer.wait`` when an attempt's paced turn comes after its call's deadline, so it is never sent.

    ``retry_after`` is the seconds until the turn the attempt would have had. It is public as
    ``headgate.ratelimit.BudgetBacklogError``.
    """

    def __init__(self, message: str, *, retry_after: float) -> None:
        """Record *message* and *retry_after*."""
        super().__init__(message)
        self.retry_after = retry_after


class BudgetPausedError(Exception):
    """Raised by ``Pacer.wait`` when the server's pause outlasts the call's deadline, so the attempt is never sent.

    ``AsyncBaseClient`` raises it to its caller as :class:`headgate.errors.RateLimitError`. ``retry_after`` is the
    seconds left in the pause; ``status_code`` is the status of the response that asked for it.
    """

    def __init__(self, message: str, *, retry_after: float, status_code: int) -> None:
        """Record *message*, *retry_after* and *status_code*."""
        super().__init__(message)
        self.retry_after = retry_after
        self.status_code = status_code
