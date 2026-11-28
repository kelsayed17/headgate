# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""``BreakerRegistry``: keyed by origin, bounded by LRU eviction of closed breakers, safe across threads.

The Hypothesis machine drives a small registry through random lookups, trips and elapsed time and checks its bounds
after every step; the threaded test runs several event loops against one registry at once.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
from collections.abc import Iterable
from typing import TYPE_CHECKING, override

import httpx2
import pytest
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule

import headgate.breaker
from headgate import BreakerPolicy
from headgate.breaker import BreakerRegistry, BreakerState
from headgate.breaker._registry import DEFAULT_MAX_HOSTS, table_of
from headgate.timing import Timing
from tests.support.timing import built_on

if TYPE_CHECKING:
    from headgate.breaker._breaker import CircuitBreaker

#: A registry small enough for Hypothesis to fill, and more origins than it holds.
_MAX = 3
_ORIGINS = tuple(f"https://h{n}.example" for n in range(_MAX + 3))

#: Thresholds of the breakers the machine and the threads build.
_THRESHOLD = 2
_COOLDOWN = 10.0

#: The threaded test: threads, origins they share, and fetches each thread makes.
_THREADS = 8
_SHARED_ORIGINS = 4
_FETCHES = 300


def _trip(breaker: CircuitBreaker) -> None:
    """Record failures until *breaker* opens."""
    while breaker.state is BreakerState.CLOSED:
        with contextlib.suppress(RuntimeError), breaker.track() as verdict:
            verdict.mark_failed()
            raise RuntimeError


def test_a_registry_shows_only_its_settings() -> None:
    """Leave finding and driving a breaker to the client: a registry shows its settings, its size and ``clear``."""
    registry: object = BreakerRegistry()

    assert [name for name in dir(BreakerRegistry) if not name.startswith("_")] == ["clear", "max_hosts", "policy"]
    assert not isinstance(registry, Iterable)


def test_circuit_breaker_is_not_exported() -> None:
    """Keep the breaker itself out of ``headgate.breaker``: it is read through ``headgate.health.breaker_states``."""
    assert "CircuitBreaker" not in headgate.breaker.__all__
    assert not hasattr(headgate.breaker, "CircuitBreaker")


def test_a_registry_holds_at_most_max_hosts_dropping_the_least_recently_used_closed_one() -> None:
    """Evict the least recently used closed breaker, keeping open ones and recently used ones."""
    registry = BreakerRegistry(BreakerPolicy(failure_threshold=1, recovery_timeout=_COOLDOWN), max_hosts=_MAX)
    first, second, third = (table_of(registry).breaker_for(o) for o in _ORIGINS[:3])
    _trip(first)
    table_of(registry).breaker_for(_ORIGINS[1])  # second is now recent
    fourth = table_of(registry).breaker_for(_ORIGINS[3])

    assert table_of(registry).breakers() == (first, second, fourth), "the closed, least recently used third was dropped"
    assert third not in table_of(registry).breakers()
    assert registry.max_hosts == _MAX


def test_a_registry_of_open_breakers_still_stays_bounded() -> None:
    """Drop the least recently used breaker of all when every one is open, so failing origins cannot grow it."""
    registry = BreakerRegistry(BreakerPolicy(failure_threshold=1, recovery_timeout=_COOLDOWN), max_hosts=1)
    first = table_of(registry).breaker_for(_ORIGINS[0])
    _trip(first)
    second = table_of(registry).breaker_for(_ORIGINS[1])
    assert table_of(registry).breakers() == (second,)


def test_a_registry_takes_an_origin_or_a_url_and_refuses_a_relative_one() -> None:
    """Accept an ``httpx2.Origin``, a URL object or text, and refuse a relative URL, which has no origin."""
    registry = BreakerRegistry()
    by_origin = table_of(registry).breaker_for(httpx2.Origin("https://a.example"))
    by_url = table_of(registry).breaker_for(httpx2.URL("https://a.example/p"))
    assert by_origin is by_url
    assert table_of(registry).breakers(httpx2.Origin("https://a.example")) == (by_origin,)
    with pytest.raises(ValueError, match="absolute"):
        table_of(registry).breaker_for("/relative")


def test_a_registry_defaults_to_a_thousand_and_twenty_four_breakers_and_refuses_none() -> None:
    """Hold 1024 breakers by default, and refuse a bound below one."""
    assert BreakerRegistry().max_hosts == DEFAULT_MAX_HOSTS
    with pytest.raises(ValueError, match="max_hosts"):
        BreakerRegistry(max_hosts=0)


def test_one_registry_shared_by_threads_running_their_own_loops_stays_consistent() -> None:
    """Run fetches from several threads, each on its own event loop, against shared breakers without corrupting them.

    Every fetch fails, so the shared breakers trip and then refuse. Each breaker's state stays one of the three, its
    failure count never passes its threshold, and every fetch either ran or was refused, none lost.
    """
    registry = BreakerRegistry(
        BreakerPolicy(failure_threshold=_THRESHOLD, recovery_timeout=_COOLDOWN), max_hosts=_SHARED_ORIGINS
    )
    outcomes: list[int] = []
    lock = threading.Lock()

    async def fetches(offset: int) -> None:
        ran = 0
        for n in range(_FETCHES):
            origin = _ORIGINS[(n + offset) % _SHARED_ORIGINS]
            breaker = table_of(registry).breaker_for(origin)
            with contextlib.suppress(Exception), breaker.track() as verdict:
                ran += 1
                await asyncio.sleep(0)
                verdict.mark_failed()
                raise RuntimeError
        with lock:
            outcomes.append(ran)

    threads = [threading.Thread(target=asyncio.run, args=(fetches(offset),)) for offset in range(_THREADS)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(outcomes) == _THREADS, "every thread finished without an error escaping"
    assert len(registry) == _SHARED_ORIGINS
    for breaker in table_of(registry).breakers():
        assert breaker.state is BreakerState.OPEN
        assert breaker.failure_count == _THRESHOLD, "a failure finishing after the trip changes nothing"
    assert sum(outcomes) < _THREADS * _FETCHES, "open breakers refused the rest"


class RegistryMachine(RuleBasedStateMachine):
    """Drive a small registry through lookups, trips, recoveries and time, checking its bounds."""

    def __init__(self) -> None:
        """Start with an empty registry on a hand-driven clock."""
        super().__init__()
        self.now = 0.0
        self.registry = built_on(
            Timing(clock=lambda: self.now),
            lambda: BreakerRegistry(
                BreakerPolicy(failure_threshold=_THRESHOLD, recovery_timeout=_COOLDOWN), max_hosts=_MAX
            ),
        )
        self.last_built: CircuitBreaker | None = None

    @rule(origin=st.sampled_from(_ORIGINS))
    def look_up(self, origin: str) -> None:
        """Fetch *origin*'s breaker, building it if new; the same origin always returns the same breaker while held."""
        held = {b.name: b for b in table_of(self.registry).breakers()}
        breaker = table_of(self.registry).breaker_for(origin)
        if origin in held:
            assert breaker is held[origin]
        assert table_of(self.registry).breakers()[-1] is breaker, "a lookup makes the breaker the most recently used"
        self.last_built = breaker

    @rule()
    def trip_the_last(self) -> None:
        """Open the breaker looked up last."""
        if self.last_built is not None and self.last_built.state is BreakerState.CLOSED:
            _trip(self.last_built)

    @rule(seconds=st.floats(min_value=0, max_value=2 * _COOLDOWN))
    def advance(self, seconds: float) -> None:
        """Let time pass."""
        self.now += seconds

    @invariant()
    def the_registry_is_bounded(self) -> None:
        """Never hold more than ``max_hosts`` breakers."""
        assert len(self.registry) <= _MAX

    @invariant()
    def keys_are_unique(self) -> None:
        """Hold one breaker per origin."""
        keys = [b.name for b in table_of(self.registry).breakers()]
        assert len(keys) == len(set(keys))

    @override
    def teardown(self) -> None:
        """Forget every breaker."""
        self.registry.clear()


TestRegistryMachine = RegistryMachine.TestCase
