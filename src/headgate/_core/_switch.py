# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""The construction-time switch that builds clients with their call policy off.

It lives here, below every layer, because a rate budget honors it when it is built, whoever builds it, and the
policy subpackages may import only this core. :mod:`headgate.policy` is its public face and documents what "off" means.
"""

from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar

__all__ = ["policy_disabled", "policy_is_disabled"]

#: Task-local: is call policy switched off for clients built right now?
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
    """Return whether a client being constructed right now should skip its call policy."""
    return _POLICY_OFF.get()
