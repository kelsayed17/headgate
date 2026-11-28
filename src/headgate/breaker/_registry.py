# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""Where a client finds its origin's breaker: a :class:`BreakerRegistry`, its own unless it is given one to share.

**Sharing.** A client given a :class:`~headgate.BreakerPolicy` builds a registry of its own from it, so its failures
open no other client's breakers. Clients that should pool their failure counts on an origin are given one registry in
its place: ``breaker=BreakerRegistry(policy)``, as clients share a ``RateBudget``. There is no process-wide registry.

**Keying.** A breaker is keyed by origin (scheme, host and effective port, :func:`headgate._core.origin_of`), and built
with the registry's policy, so every breaker it holds trips as every client sharing it expects.

**Bounds.** A registry holds at most ``max_hosts`` breakers. Past that, the least recently used closed breaker is
dropped (a closed breaker holds at most a few failures, which a fresh one relearns); only when every breaker is open
or half-open is the least recently used of all dropped, so a stream of distinct failing origins cannot grow it without
bound either.

**Mechanism.** A registry shows its settings, its size and ``clear``; the breakers live in its
:class:`BreakerTable`, which only the client (and :mod:`headgate.health`) reaches, through :func:`table_of`, as the
rate layer reaches a ``RateBudget``'s pacer.

**Threads.** The breakers and their recency order are guarded by a ``threading.Lock``, so clients on different event
loops in different threads can share a registry; each breaker guards its own state (see
:mod:`headgate.breaker._breaker`).
"""

import threading
import weakref
from collections import OrderedDict
from collections.abc import Generator
from contextlib import AbstractContextManager, contextmanager
from typing import Final, NoReturn, Protocol, Self, final, override

import httpx2

from headgate._core import (
    BreakerState,
    Clock,
    Timing,
    check_int,
    check_kind,
    current_timing,
    origin_of,
    origin_text,
    refuse_pickle,
    sealed,
    timing_override,
)
from headgate.breaker._breaker import CircuitBreaker, TransitionListener, Verdict
from headgate.breaker._policy import BreakerPolicy

__all__ = ["DEFAULT_MAX_HOSTS", "NO_BREAKER", "Breaker", "BreakerRegistry", "BreakerTable", "table_of"]

#: How many breakers a registry holds by default before it drops the least recently used closed one.
DEFAULT_MAX_HOSTS: Final = 1024

#: The policy a registry built without one holds.
_DEFAULT_POLICY: Final = BreakerPolicy()

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


class BreakerTable:
    """The breakers of a :class:`BreakerRegistry`, by origin, built on first use with its policy. See the module.

    What a client finds its origin's breaker in, through :func:`table_of`. Reading it (``len``, :meth:`breakers`) never
    builds a breaker.
    """

    __slots__ = ("_breakers", "_lock", "_max_hosts", "_policy", "_timing")

    def __init__(self, policy: BreakerPolicy, max_hosts: int, timing: Timing) -> None:
        """Start empty, building at most *max_hosts* breakers with *policy*'s thresholds, on *timing*."""
        self._policy = policy
        self._max_hosts = max_hosts
        self._timing = timing
        self._lock = threading.Lock()
        self._breakers: OrderedDict[_Key, CircuitBreaker] = OrderedDict()

    @property
    def policy(self) -> BreakerPolicy:
        """The thresholds every breaker is built with."""
        return self._policy

    @property
    def max_hosts(self) -> int:
        """How many breakers the table holds at most."""
        return self._max_hosts

    @property
    def clock(self) -> Clock:
        """The clock every breaker's cooldown runs on: the timing's current when the registry was built."""
        return self._timing.clock

    def breaker_for(self, origin: httpx2.Origin | httpx2.URL | str) -> CircuitBreaker:
        """Return *origin*'s breaker (an origin, or an absolute URL), building it with the policy if new.

        :raises ValueError: for a relative URL, which has no origin.
        """
        key = name = origin_text(origin if isinstance(origin, httpx2.Origin) else origin_of(origin))
        with self._lock:
            breaker = self._breakers.get(key)
            if breaker is not None:
                self._breakers.move_to_end(key)
                return breaker
            # A breaker reads the current timing when it is built: this one is built on the registry's.
            with timing_override(self._timing):
                breaker = self._breakers[key] = CircuitBreaker(
                    failure_threshold=self._policy.failure_threshold,
                    recovery_timeout=self._policy.recovery_timeout,
                    name=name,
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

    def __len__(self) -> int:
        """Return how many breakers the table holds."""
        return len(self._breakers)

    def clear(self) -> None:
        """Forget every breaker, so each origin starts closed again."""
        with self._lock:
            self._breakers.clear()


@final
@sealed("share one registry between the clients whose breakers it pools")
class BreakerRegistry:
    """The breakers of every origin its clients have called, built on first use with its *policy*. See the module.

    Every client given the same registry as its ``breaker=`` shares its breakers; read their states with
    :func:`headgate.health.breaker_snapshots`, which builds none. *max_hosts* bounds how many it holds; every breaker it
    builds runs on the timing current when the registry is built (:func:`headgate.timing.current_timing`), whenever it
    builds it.
    """

    __slots__ = ("__weakref__", "_table")

    def __init__(
        self,
        policy: BreakerPolicy = _DEFAULT_POLICY,
        *,
        max_hosts: int = DEFAULT_MAX_HOSTS,
    ) -> None:
        """Start empty, building each breaker with *policy*'s thresholds.

        :raises TypeError: for a *policy* that is not a :class:`~headgate.BreakerPolicy`, and a *max_hosts* that is not
            an ``int``.
        :raises ValueError: for a *max_hosts* below one.
        """
        check_kind("policy", policy, BreakerPolicy, "a BreakerPolicy, such as BreakerPolicy(failure_threshold=3)")
        check_int("max_hosts", max_hosts, "a number of origins, an int of at least 1")
        if max_hosts < 1:
            msg = f"max_hosts must be >= 1 (got {max_hosts})"
            raise ValueError(msg)
        self._table = _TABLES[self] = BreakerTable(policy, max_hosts, current_timing())

    @property
    def policy(self) -> BreakerPolicy:
        """When the registry's breakers trip and test their origin again."""
        return self._table.policy

    @property
    def max_hosts(self) -> int:
        """How many breakers the registry holds at most."""
        return self._table.max_hosts

    def __len__(self) -> int:
        """Return how many breakers the registry holds."""
        return len(self._table)

    def clear(self) -> None:
        """Forget every breaker, so each origin starts closed again."""
        self._table.clear()

    def __copy__(self) -> Self:
        """Return this registry itself: a copy of a shared handle is the handle, so its holders share it."""
        return self

    def __deepcopy__(self, memo: dict[int, object]) -> Self:
        """Return this registry itself, as :meth:`__copy__` does, so a deep copy of a policy keeps sharing it."""
        del memo
        return self

    @override
    def __reduce__(self) -> NoReturn:
        """Refuse to be pickled: a live registry is state its clients share in one process, raising ``TypeError``."""
        refuse_pickle(self)

    @override
    def __repr__(self) -> str:
        """Return the registry's policy, its bound and each origin's breaker state."""
        states = {breaker.name: str(breaker.state) for breaker in self._table.breakers()}
        return f"BreakerRegistry({self.policy!r}, max_hosts={self.max_hosts}, breakers={states!r})"


#: Each live registry's table. Weak, so a registry nobody holds any more goes with its breakers.
_TABLES: Final[weakref.WeakKeyDictionary[BreakerRegistry, BreakerTable]] = weakref.WeakKeyDictionary()


def table_of(registry: BreakerRegistry) -> BreakerTable:
    """Return the table *registry*'s breakers live in."""
    return _TABLES[registry]
