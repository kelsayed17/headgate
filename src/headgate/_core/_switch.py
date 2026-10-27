# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""The construction-time switch that builds clients with their transport policy off.

It lives here, below every layer, because a rate budget honors it when it is built, whoever builds it, and the
policy subpackages may import only this core. :mod:`headgate.policy` is its public face and documents what "off" means.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Generator

__all__ = ["policy_disabled", "policy_is_disabled"]

#: Task-local: is transport policy switched off for clients built right now?
_POLICY_OFF: ContextVar[bool] = ContextVar("headgate_policy_off", default=False)


@contextmanager
def policy_disabled() -> Generator[None]:
    """Build clients with retry, the circuit breaker, and rate limiting switched off.

    Affects only clients constructed inside the block. Safe to nest.
    """
    token = _POLICY_OFF.set(True)
    try:
        yield
    finally:
        _POLICY_OFF.reset(token)


def policy_is_disabled() -> bool:
    """Return whether a client being constructed right now should skip its transport policy."""
    return _POLICY_OFF.get()
