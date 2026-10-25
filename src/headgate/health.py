"""Read-only snapshots of the transport tier's degradation state, for an application to export.

This package neither logs nor emits metrics. Two pieces of state that say a host or the cache is degraded live here
anyway, process-global: the per-host circuit breakers and the count of requests the cache handed to the live transport
because it broke. Without these, an open breaker reaches someone only as the ``TransportError`` its caller saw.

These two functions, and the snapshot type the first returns, are the whole read surface. Each returns a fresh copy, so
a caller can neither mutate the registry nor see it change mid-iteration; a metrics callback can call them on a timer.
"""

from __future__ import annotations

from typing import NamedTuple

from headgate._breaker import HOST_BREAKERS, BreakerState
from headgate._cache import CACHE_DEGRADATIONS

__all__ = ["BreakerSnapshot", "BreakerState", "breaker_states", "cache_degradations"]


class BreakerSnapshot(NamedTuple):
    """One host breaker at one instant.

    ``state`` alone is not enough to say whether the host is being refused: an OPEN breaker whose cooldown has
    elapsed still reads OPEN until some call takes the probe, so a host called only by a daily cron would read
    OPEN all day. ``refusing`` is whether a call made now would be turned away.
    """

    state: BreakerState
    refusing: bool


def breaker_states() -> dict[str, BreakerSnapshot]:
    """Every host breaker built so far. A host no client has called yet is absent."""
    return {host: BreakerSnapshot(breaker.state, breaker.refusing) for host, breaker in tuple(HOST_BREAKERS.items())}


def cache_degradations() -> dict[str, int]:
    """Per host, how many requests found the HTTP cache broken and were handed to the live transport."""
    return dict(CACHE_DEGRADATIONS)
