# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""A context-local replacement for the default breaker registry, for the clients built inside a block.

A client class usually fixes its own ``BreakerPolicy`` in ``super().__init__()``, so a test cannot hand it a registry
of its own. :func:`registry_override` makes every client constructed in its block that would use
:data:`~headgate.breaker.DEFAULT_REGISTRY` use the given registry instead, the way
:func:`headgate._core.timing_override` replaces the clock; the client asks :func:`scoped_registry` which registry that
is. A client configured with a registry of its own keeps it. ``headgate.testing.isolated_breakers`` is the public face.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING

from headgate.breaker._registry import DEFAULT_REGISTRY, BreakerRegistry

if TYPE_CHECKING:
    from collections.abc import Generator

__all__ = ["registry_override", "scoped_registry"]

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


def scoped_registry(registry: BreakerRegistry) -> BreakerRegistry:
    """Return the registry a client built now uses for *registry*: the block's override in place of the default one."""
    override = _OVERRIDE.get()
    return override if override is not None and registry is DEFAULT_REGISTRY else registry
