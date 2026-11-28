# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""The implementation of :mod:`headgate.health`, which exports it and documents it."""

from dataclasses import dataclass

from headgate._core import OBSERVER_FAILURES, BreakerState
from headgate.breaker import BreakerRegistry
from headgate.breaker._registry import table_of

__all__ = ["BreakerSnapshot", "breaker_states", "observer_failures"]


@dataclass(frozen=True, slots=True, kw_only=True)
class BreakerSnapshot:
    """One origin's breaker at one instant.

    ``state`` alone is not enough to say whether the origin is being refused: an OPEN breaker whose cooldown has
    elapsed still reads OPEN until some call takes the probe, so an origin called only by a daily cron would read
    OPEN all day. ``refusing`` is whether a call made now would be turned away. ``failures`` is how many consecutive
    failures the breaker has recorded since its last success; a breaker opens when they reach its policy's
    ``failure_threshold``.
    """

    state: BreakerState
    refusing: bool
    failures: int


def breaker_states(registry: BreakerRegistry) -> dict[str, BreakerSnapshot]:
    """Return a snapshot per origin (``scheme://host[:port]``) *registry* holds a breaker for.

    Pass the registry the clients to report on share (their ``breaker=BreakerRegistry(...)``); a client given a
    ``BreakerPolicy`` keeps its breakers to itself. An origin no client has called yet is absent.
    """
    return {
        breaker.name: BreakerSnapshot(state=breaker.state, refusing=breaker.refusing, failures=breaker.failure_count)
        for breaker in table_of(registry).breakers()
    }


def observer_failures() -> dict[str, int]:
    """Return, per observer class, how many events its ``notify`` raised on, which headgate swallowed.

    The count is process-wide, across every client: an observer that raises cannot be told about its own failure
    through observers, so this is where it is read.
    """
    return dict(OBSERVER_FAILURES)
