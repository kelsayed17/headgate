# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""Where a client finds its host's breaker: a :class:`BreakerRegistry`, by default the process-wide one."""

from __future__ import annotations

import time
from contextlib import contextmanager
from typing import TYPE_CHECKING, Protocol

from headgate.breaker._breaker import CircuitBreaker, Verdict

if TYPE_CHECKING:
    from collections.abc import Generator, Iterator
    from contextlib import AbstractContextManager

    from headgate._core import Clock

__all__ = ["DEFAULT_REGISTRY", "NO_BREAKER", "Breaker", "BreakerRegistry"]


class Breaker(Protocol):
    """What a client needs of a breaker: one ``track()`` scope per fetch; :class:`CircuitBreaker` is the real one."""

    def track(self) -> AbstractContextManager[Verdict]:
        """Run one fetch through the breaker, yielding the :class:`Verdict` it marks."""
        ...


class _NoBreaker:
    """A breaker that admits every call and records nothing: what a client built with ``breaker=None`` uses.

    It never reads a registry, and that is half of what "no breaker" means: a registry is shared, so a client that
    merely stopped recording would still be refused by a breaker another client opened on the same host.
    """

    __slots__ = ()

    @contextmanager
    def track(self) -> Generator[Verdict]:
        """Admit the call; the verdict it marks is discarded."""
        yield Verdict()


#: The breaker of a client with no breaker. See :class:`_NoBreaker`.
NO_BREAKER: Breaker = _NoBreaker()


class BreakerRegistry:
    """The breakers of every host its clients have called, keyed by host, built on first use.

    Every client whose ``BreakerPolicy`` names the same registry shares its breakers. The first client to address a
    host builds that host's breaker with its own policy's threshold and cooldown, and later clients share it as built.
    Reading the registry (``registry[host]``, ``in``, iteration) never builds a breaker. *clock* is the monotonic clock
    every breaker it builds reads.
    """

    __slots__ = ("_breakers", "_clock")

    def __init__(self, *, clock: Clock = time.monotonic) -> None:
        """Start empty; the breakers built here read *clock*."""
        self._clock = clock
        self._breakers: dict[str, CircuitBreaker] = {}

    def breaker_for(self, host: str, *, failure_threshold: int, recovery_timeout: float) -> CircuitBreaker:
        """Return *host*'s breaker, building it with *failure_threshold* and *recovery_timeout* when it has none."""
        breaker = self._breakers.get(host)
        if breaker is None:
            breaker = self._breakers[host] = CircuitBreaker(
                failure_threshold=failure_threshold, recovery_timeout=recovery_timeout, name=host, clock=self._clock
            )
        return breaker

    def __getitem__(self, host: str) -> CircuitBreaker:
        """Return *host*'s breaker; ``KeyError`` when no client has called it."""
        return self._breakers[host]

    def __contains__(self, host: object) -> bool:
        """Return whether *host* has a breaker."""
        return host in self._breakers

    def __iter__(self) -> Iterator[str]:
        """Iterate over the hosts that have a breaker, from a snapshot."""
        return iter(tuple(self._breakers))

    def __len__(self) -> int:
        """Return how many hosts have a breaker."""
        return len(self._breakers)

    def items(self) -> tuple[tuple[str, CircuitBreaker], ...]:
        """Return a snapshot of every ``(host, breaker)`` pair."""
        return tuple(self._breakers.items())

    def clear(self) -> None:
        """Forget every breaker, so each host starts closed again."""
        self._breakers.clear()


#: The registry every client shares unless its ``BreakerPolicy`` names another, so concurrent clients to one host share
#: a breaker. ``headgate.health.breaker_states`` reads it.
DEFAULT_REGISTRY = BreakerRegistry()
