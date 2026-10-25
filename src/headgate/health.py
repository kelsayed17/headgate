# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""Read-only snapshots of the transport tier's degradation state, for an application to export.

This package never logs. Three pieces of process-global state that say something is degraded are readable here: the
circuit breakers of the default :class:`~headgate.breaker.BreakerRegistry`, the count of requests the cache handed to
the live transport because it broke, and the count of observer failures headgate swallowed. Without these, an open
breaker reaches someone only as the ``CircuitOpenError`` its caller saw.

These functions, and the snapshot type the first returns, are the whole read surface. Each returns a fresh copy, so a
caller can neither mutate the state nor see it change mid-iteration; a metrics callback can call them on a timer.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple

from headgate._cache import CACHE_DEGRADATIONS
from headgate._core import OBSERVER_FAILURES, BreakerState
from headgate.breaker import DEFAULT_REGISTRY

if TYPE_CHECKING:
    from headgate.breaker import BreakerRegistry

__all__ = ["BreakerSnapshot", "BreakerState", "breaker_states", "cache_degradations", "observer_failures"]


class BreakerSnapshot(NamedTuple):
    """One host breaker at one instant.

    ``state`` alone is not enough to say whether the host is being refused: an OPEN breaker whose cooldown has
    elapsed still reads OPEN until some call takes the probe, so a host called only by a daily cron would read
    OPEN all day. ``refusing`` is whether a call made now would be turned away.
    """

    state: BreakerState
    refusing: bool


def breaker_states(registry: BreakerRegistry = DEFAULT_REGISTRY) -> dict[str, BreakerSnapshot]:
    """Return a snapshot of every host breaker *registry* has built; a host no client has called yet is absent."""
    return {host: BreakerSnapshot(breaker.state, breaker.refusing) for host, breaker in registry.items()}


def cache_degradations() -> dict[str, int]:
    """Return, per host, how many requests found the HTTP cache broken and were handed to the live transport."""
    return dict(CACHE_DEGRADATIONS)


def observer_failures() -> dict[str, int]:
    """Return, per observer class, how many events its ``notify`` raised on, which headgate swallowed."""
    return dict(OBSERVER_FAILURES)
