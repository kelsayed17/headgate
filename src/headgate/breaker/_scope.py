# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""A context-local replacement for the default breaker registry, for the clients built inside a block.

A client class usually fixes its own ``TransportPolicy`` in ``super().__init__()``, so a test cannot hand it a
registry of its own. :func:`registry_override` makes every client constructed in its block that would use
:data:`~headgate.breaker.DEFAULT_REGISTRY` use the given registry instead, the way
:func:`headgate._core.timing_override` replaces the clock. A client configured with a registry of its own keeps it.
``headgate.testing.isolated_breakers`` is the public face.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import replace
from typing import TYPE_CHECKING

from headgate.breaker import DEFAULT_REGISTRY, BreakerRegistry

if TYPE_CHECKING:
    from collections.abc import Generator

    from headgate._options import TransportPolicy

__all__ = ["registry_override", "scoped_breakers"]

#: The registry clients built in this context take in place of the default one, if any.
_OVERRIDE: ContextVar[BreakerRegistry | None] = ContextVar("headgate_breaker_registry", default=None)


@contextmanager
def registry_override(registry: BreakerRegistry) -> Generator[BreakerRegistry]:
    """Build clients that would use the default registry with *registry* instead, inside the block; yield it."""
    token = _OVERRIDE.set(registry)
    try:
        yield registry
    finally:
        _OVERRIDE.reset(token)


def scoped_breakers(policy: TransportPolicy) -> TransportPolicy:
    """Return *policy* with the overriding registry in place of the default one, when a block set one."""
    override = _OVERRIDE.get()
    breaker = policy.breaker
    if override is None or breaker is None or breaker.registry is not DEFAULT_REGISTRY:
        return policy
    return replace(policy, breaker=replace(breaker, registry=override))
