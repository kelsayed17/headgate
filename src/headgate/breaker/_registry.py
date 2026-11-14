# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""Where a client finds its origin's breaker: a :class:`BreakerRegistry`, its own unless it is given one to share.

**Sharing.** A client builds a registry of its own, so its failures open no other client's breakers. Clients that
should pool their failure counts on an origin are given one registry: ``BreakerPolicy(registry=shared)``. There is no
process-wide registry.

**Keying.** A breaker is keyed by origin (scheme, host and effective port, :func:`headgate._core.origin_of`). A registry
fixes its thresholds, ``failure_threshold`` and ``recovery_timeout``, the first time a client binds it or a breaker is
asked of it, and refuses a client whose policy asks for others with ``ValueError`` when that client is built, so
every breaker it holds trips as every client sharing it expects.

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
from collections import OrderedDict
from contextlib import contextmanager
from typing import TYPE_CHECKING, Final, Protocol, override

import httpx2

from headgate._core import BreakerState, current_timing, origin_of, origin_text
from headgate.breaker._breaker import CircuitBreaker, Verdict

if TYPE_CHECKING:
    from collections.abc import Generator, Iterator
    from contextlib import AbstractContextManager

    from headgate._core import Timing
    from headgate.breaker._breaker import TransitionListener

__all__ = ["DEFAULT_MAX_HOSTS", "NO_BREAKER", "Breaker", "BreakerRegistry"]

#: How many breakers a registry holds by default before it drops the least recently used closed one.
DEFAULT_MAX_HOSTS: Final = 1024

#: An origin, as the text a breaker is named by.
type _Key = str


class Breaker(Protocol):
    """What a client needs of a breaker; :class:`CircuitBreaker` is the real one."""

    def track(self, on_change: TransitionListener | None = None) -> AbstractContextManager[Verdict]:
        """Run one fetch through the breaker, yielding the :class:`Verdict` it marks."""
        ...


class _NoBreaker:
    """A breaker that admits every call and records nothing: what a client built with ``breaker=None`` uses.

    It never reads a registry, and that is half of what "no breaker" means: a registry is shared, so a client that
    merely stopped recording would still be refused by a breaker another client opened on the same origin.
    """

    __slots__ = ()

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
    iteration, :meth:`breakers`) never builds one. *max_hosts* bounds how many it holds; every breaker it builds runs
    on *timing*, or, without one, on the timing current when the registry is built.
    """

    __slots__ = ("_breakers", "_lock", "_max_hosts", "_thresholds", "_timing")

    def __init__(self, *, max_hosts: int = DEFAULT_MAX_HOSTS, timing: Timing | None = None) -> None:
        """Start empty.

        :raises ValueError: for a *max_hosts* below one.
        """
        if max_hosts < 1:
            msg = f"max_hosts must be >= 1 (got {max_hosts})"
            raise ValueError(msg)
        self._max_hosts = max_hosts
        self._timing = current_timing() if timing is None else timing
        self._thresholds: tuple[int, float] | None = None
        self._lock = threading.Lock()
        self._breakers: OrderedDict[_Key, CircuitBreaker] = OrderedDict()

    @property
    def max_hosts(self) -> int:
        """How many breakers the registry holds at most."""
        return self._max_hosts

    def bind(self, *, failure_threshold: int, recovery_timeout: float) -> None:
        """Fix the thresholds of every breaker this registry builds, or check them against the ones already fixed.

        A client calls this when it is built, so a policy that cannot share the registry fails there, before any call.

        :raises ValueError: for thresholds other than the ones the registry was first bound with, naming both.
        """
        asked = (failure_threshold, recovery_timeout)
        with self._lock:
            if self._thresholds is None:
                self._thresholds = asked
            elif asked != self._thresholds:
                fixed_threshold, fixed_timeout = self._thresholds
                msg = (
                    f"this BreakerRegistry's breakers trip after {fixed_threshold} failures with a {fixed_timeout:g}s "
                    f"cooldown, so a client with BreakerPolicy(failure_threshold={failure_threshold}, "
                    f"recovery_timeout={recovery_timeout:g}) cannot share it: give that client the same thresholds, "
                    "or a registry of its own"
                )
                raise ValueError(msg)

    def breaker_for(
        self, origin: httpx2.Origin | httpx2.URL | str, *, failure_threshold: int, recovery_timeout: float
    ) -> CircuitBreaker:
        """Return *origin*'s breaker (an origin, or an absolute URL), building it with these thresholds if new.

        :raises ValueError: for a relative URL, which has no origin, and for thresholds the registry is not bound to
            (see :meth:`bind`).
        """
        name = origin_text(origin if isinstance(origin, httpx2.Origin) else origin_of(origin))
        self.bind(failure_threshold=failure_threshold, recovery_timeout=recovery_timeout)
        key = name
        with self._lock:
            breaker = self._breakers.get(key)
            if breaker is not None:
                self._breakers.move_to_end(key)
                return breaker
            breaker = self._breakers[key] = CircuitBreaker(
                failure_threshold=failure_threshold, recovery_timeout=recovery_timeout, name=name, timing=self._timing
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
        """Forget every breaker, so each origin starts closed again; the thresholds stay fixed."""
        with self._lock:
            self._breakers.clear()

    @override
    def __repr__(self) -> str:
        states = {breaker.name: str(breaker.state) for breaker in self.breakers()}
        return f"BreakerRegistry({states!r})"
