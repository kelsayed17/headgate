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

from headgate._core import OBSERVER_FAILURES, BreakerState
from headgate.breaker import DEFAULT_REGISTRY
from headgate.cache._transport import CACHE_DEGRADATIONS

if TYPE_CHECKING:
    from headgate.breaker import BreakerRegistry

__all__ = ["BreakerSnapshot", "BreakerState", "breaker_states", "cache_degradations", "observer_failures"]


class BreakerSnapshot(NamedTuple):
    """One origin's breaker at one instant.

    ``state`` alone is not enough to say whether the origin is being refused: an OPEN breaker whose cooldown has
    elapsed still reads OPEN until some call takes the probe, so an origin called only by a daily cron would read
    OPEN all day. ``refusing`` is whether a call made now would be turned away.
    """

    state: BreakerState
    refusing: bool


#: How degraded each state is, for choosing which of an origin's breakers its snapshot reports.
_SEVERITY = {BreakerState.CLOSED: 0, BreakerState.HALF_OPEN: 1, BreakerState.OPEN: 2}


def breaker_states(registry: BreakerRegistry = DEFAULT_REGISTRY) -> dict[str, BreakerSnapshot]:
    """Return a snapshot per origin (``scheme://host[:port]``) *registry* holds a breaker for.

    Clients whose breaker policies differ in their thresholds keep separate breakers for one origin; the origin's
    snapshot is then the most degraded of them (refusing first, then open, half-open, closed). An origin no client has
    called yet is absent.
    """
    snapshots: dict[str, BreakerSnapshot] = {}
    for breaker in registry.breakers():
        snapshot = BreakerSnapshot(breaker.state, breaker.refusing)
        seen = snapshots.get(breaker.name)
        if seen is None or _rank(snapshot) > _rank(seen):
            snapshots[breaker.name] = snapshot
    return snapshots


def _rank(snapshot: BreakerSnapshot) -> tuple[bool, int]:
    """Order snapshots from healthy to degraded."""
    return snapshot.refusing, _SEVERITY[snapshot.state]


def cache_degradations() -> dict[str, int]:
    """Return, per host, how many requests found the HTTP cache broken and were handed to the live transport."""
    return dict(CACHE_DEGRADATIONS)


def observer_failures() -> dict[str, int]:
    """Return, per observer class, how many events its ``notify`` raised on, which headgate swallowed."""
    return dict(OBSERVER_FAILURES)
