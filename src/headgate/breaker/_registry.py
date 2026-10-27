# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""Where a client finds its origin's breaker: a :class:`BreakerRegistry`, by default the process-wide one.

**Keying.** A breaker is keyed by origin (scheme, host and effective port, :func:`headgate._core.origin_of`) and by the
policy's ``failure_threshold`` and ``recovery_timeout``. Every client that addresses an origin with the same thresholds
shares one breaker; a client that asks for different thresholds gets a breaker of its own for that origin, with exactly
the thresholds it asked for. The alternative, the first client to address an origin fixing the thresholds every later
client gets, makes a client's breaker behavior depend on which client happened to call first.

**Bounds.** A registry holds at most ``max_hosts`` breakers. Past that, the least recently used closed breaker is
dropped (a closed breaker holds at most a few failures, which a fresh one relearns); only when every breaker is open
or half-open is the least recently used of all dropped, so a stream of distinct failing origins cannot grow it without
bound either.

**Threads.** The breakers and their recency order are guarded by a ``threading.Lock``, so clients on different event
loops in different threads can share a registry; each breaker guards its own state (see
:mod:`headgate.breaker._breaker`).
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from contextlib import contextmanager
from typing import TYPE_CHECKING, Final, Protocol

import httpx2

from headgate._core import BreakerState, origin_of, origin_text
from headgate.breaker._breaker import CircuitBreaker, Verdict

if TYPE_CHECKING:
    from collections.abc import Generator, Iterator
    from contextlib import AbstractContextManager

    from headgate._core import Clock
    from headgate.breaker._breaker import TransitionListener

__all__ = ["DEFAULT_MAX_HOSTS", "DEFAULT_REGISTRY", "NO_BREAKER", "Breaker", "BreakerRegistry"]

#: How many breakers a registry holds by default before it drops the least recently used closed one.
DEFAULT_MAX_HOSTS: Final = 1024

type _Key = tuple[str, int, float]


class Breaker(Protocol):
    """What a client needs of a breaker; :class:`CircuitBreaker` is the real one."""

    @property
    def refusing(self) -> bool:
        """Whether a call made now would be refused."""
        ...

    def track(self, on_change: TransitionListener | None = None) -> AbstractContextManager[Verdict]:
        """Run one fetch through the breaker, yielding the :class:`Verdict` it marks."""
        ...


class _NoBreaker:
    """A breaker that admits every call and records nothing: what a client built with ``breaker=None`` uses.

    It never reads a registry, and that is half of what "no breaker" means: a registry is shared, so a client that
    merely stopped recording would still be refused by a breaker another client opened on the same origin.
    """

    __slots__ = ()

    @property
    def refusing(self) -> bool:
        """Never refuse."""
        return False

    @contextmanager
    def track(self, on_change: TransitionListener | None = None) -> Generator[Verdict]:
        """Admit the call; the verdict it marks is discarded, and nothing changes for *on_change* to hear about."""
        del on_change
        yield Verdict()


#: The breaker of a client with no breaker. See :class:`_NoBreaker`.
NO_BREAKER: Breaker = _NoBreaker()


class BreakerRegistry:
    """The breakers of every origin its clients have called, built on first use. See the module docstring.

    Every client whose ``BreakerPolicy`` names the same registry shares its breakers. Reading the registry (``len``,
    iteration, :meth:`breakers`) never builds one. *max_hosts* bounds how many it holds; *clock* is the monotonic clock
    every breaker it builds reads.
    """

    __slots__ = ("_breakers", "_clock", "_lock", "_max_hosts")

    def __init__(self, *, max_hosts: int = DEFAULT_MAX_HOSTS, clock: Clock = time.monotonic) -> None:
        """Start empty.

        :raises ValueError: for a *max_hosts* below one.
        """
        if max_hosts < 1:
            msg = f"max_hosts must be >= 1 (got {max_hosts})"
            raise ValueError(msg)
        self._max_hosts = max_hosts
        self._clock = clock
        self._lock = threading.Lock()
        self._breakers: OrderedDict[_Key, CircuitBreaker] = OrderedDict()

    @property
    def max_hosts(self) -> int:
        """How many breakers the registry holds at most."""
        return self._max_hosts

    def breaker_for(
        self, origin: httpx2.Origin | httpx2.URL | str, *, failure_threshold: int, recovery_timeout: float
    ) -> CircuitBreaker:
        """Return the breaker for *origin* (an origin, or an absolute URL) with these thresholds, building it if new.

        :raises ValueError: for a relative URL, which has no origin.
        """
        name = origin_text(origin if isinstance(origin, httpx2.Origin) else origin_of(origin))
        key = (name, failure_threshold, recovery_timeout)
        with self._lock:
            breaker = self._breakers.get(key)
            if breaker is not None:
                self._breakers.move_to_end(key)
                return breaker
            breaker = self._breakers[key] = CircuitBreaker(
                failure_threshold=failure_threshold, recovery_timeout=recovery_timeout, name=name, clock=self._clock
            )
            if len(self._breakers) > self._max_hosts:
                self._evict(keep=key)
            return breaker

    def _evict(self, *, keep: _Key) -> None:
        """Drop the least recently used closed breaker other than *keep*, else the least recently used of all."""
        candidates = [key for key in self._breakers if key != keep]
        closed = (key for key in candidates if self._breakers[key].state is BreakerState.CLOSED)
        del self._breakers[next(closed, candidates[0])]

    def breakers(self, origin: httpx2.Origin | httpx2.URL | str | None = None) -> tuple[CircuitBreaker, ...]:
        """Return a snapshot of the breakers, least recently used first; only *origin*'s when one is given."""
        with self._lock:
            found = tuple(self._breakers.values())
        if origin is None:
            return found
        name = origin_text(origin if isinstance(origin, httpx2.Origin) else origin_of(origin))
        return tuple(breaker for breaker in found if breaker.name == name)

    def __iter__(self) -> Iterator[CircuitBreaker]:
        """Iterate over a snapshot of the breakers, least recently used first."""
        return iter(self.breakers())

    def __len__(self) -> int:
        """Return how many breakers the registry holds."""
        return len(self._breakers)

    def clear(self) -> None:
        """Forget every breaker, so each origin starts closed again."""
        with self._lock:
            self._breakers.clear()


#: The registry every client shares unless its ``BreakerPolicy`` names another, so concurrent clients to one origin
#: share a breaker. ``headgate.health.breaker_states`` reads it.
DEFAULT_REGISTRY = BreakerRegistry()
