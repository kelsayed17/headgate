# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""Seams two policy areas meet at, defined once so neither has to import the other.

* :class:`CredentialStamper`: what an auth declares about the credentials it puts on a request. The credential guard
  uses it to keep them on trusted origins; the cache uses it to partition entries by credential and to keep
  credentials out of storage.
* :data:`BREAKER_GATE`: set by the client on every call, read by the retry layer, so a retry sequence stops as soon as
  the host's breaker opens without the retry layer knowing about breakers.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, runtime_checkable

from headgate._core._context import Slot

__all__ = ["BREAKER_GATE", "CredentialStamper"]


@runtime_checkable
class CredentialStamper(Protocol):
    """An auth that declares the credentials it stamps on a request."""

    @property
    def credential_headers(self) -> frozenset[str]:
        """Return the names of the headers this auth sets (compared case-insensitively)."""
        ...

    @property
    def credential_params(self) -> frozenset[str]:
        """Return the names of the query parameters this auth sets."""
        ...

    def credential_identity(self) -> bytes:
        """Return stable bytes identifying the credential (its secret material); never stored, only hashed."""
        ...


#: Answers whether the breaker for the call's origin is refusing right now. The client puts it on every call's
#: context; the retry layer checks it before scheduling another attempt.
BREAKER_GATE = Slot[Callable[[], bool]]("breaker_gate")
