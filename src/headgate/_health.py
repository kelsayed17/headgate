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
    """Return a snapshot per origin *registry* holds a breaker for, each read at one instant.

    Each key is the origin in its canonical form, ``scheme://host[:port]``: the host lower-cased, an IPv6 host in
    brackets, and the port left out when it is the scheme's default. A client on ``https://API.example:443/v1`` is
    keyed ``"https://api.example"``. An origin no client has called yet is absent, so read one with
    ``.get("https://api.example")``.

    Pass the registry the clients to report on share (their ``breaker=BreakerRegistry(...)``); a client given a
    ``BreakerPolicy`` keeps its breakers to itself.
    """
    snapshots: dict[str, BreakerSnapshot] = {}
    for breaker in table_of(registry).breakers():
        state, refusing, failures = breaker.reading()
        snapshots[breaker.name] = BreakerSnapshot(state=state, refusing=refusing, failures=failures)
    return snapshots


def observer_failures() -> dict[str, int]:
    """Return, per observer class, how many events its ``notify`` raised on, which headgate swallowed.

    Each key is the class's module and qualified name, such as ``"myapp.metrics.Exporter"``.

    The count is process-wide, across every client: an observer that raises cannot be told about its own failure
    through observers, so this is where it is read.
    """
    return dict(OBSERVER_FAILURES)
