# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""``BreakerRegistry``: keyed by origin and thresholds, bounded by LRU eviction of closed breakers, safe across threads.

The Hypothesis machine drives a small registry through random lookups, trips and elapsed time and checks its bounds
after every step; the threaded test runs several event loops against one registry at once.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
from typing import override

import httpx2
import pytest
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule

from headgate.breaker import DEFAULT_MAX_HOSTS, BreakerRegistry, BreakerState, CircuitBreaker

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


def test_a_registry_holds_at_most_max_hosts_dropping_the_least_recently_used_closed_one() -> None:
    """Evict the least recently used closed breaker, keeping open ones and recently used ones."""
    registry = BreakerRegistry(max_hosts=_MAX)
    first, second, third = (
        registry.breaker_for(o, failure_threshold=1, recovery_timeout=_COOLDOWN) for o in _ORIGINS[:3]
    )
    _trip(first)
    registry.breaker_for(_ORIGINS[1], failure_threshold=1, recovery_timeout=_COOLDOWN)  # second is now recent
    fourth = registry.breaker_for(_ORIGINS[3], failure_threshold=1, recovery_timeout=_COOLDOWN)

    assert registry.breakers() == (first, second, fourth), "the closed, least recently used third was dropped"
    assert third not in registry.breakers()
    assert registry.max_hosts == _MAX


def test_a_registry_of_open_breakers_still_stays_bounded() -> None:
    """Drop the least recently used breaker of all when every one is open, so failing origins cannot grow it."""
    registry = BreakerRegistry(max_hosts=1)
    first = registry.breaker_for(_ORIGINS[0], failure_threshold=1, recovery_timeout=_COOLDOWN)
    _trip(first)
    second = registry.breaker_for(_ORIGINS[1], failure_threshold=1, recovery_timeout=_COOLDOWN)
    assert registry.breakers() == (second,)


def test_a_registry_takes_an_origin_or_a_url_and_refuses_a_relative_one() -> None:
    """Accept an ``httpx2.Origin``, a URL object or text, and refuse a relative URL, which has no origin."""
    registry = BreakerRegistry()
    by_origin = registry.breaker_for(httpx2.Origin("https://a.example"), failure_threshold=1, recovery_timeout=1.0)
    by_url = registry.breaker_for(httpx2.URL("https://a.example/p"), failure_threshold=1, recovery_timeout=1.0)
    assert by_origin is by_url
    assert registry.breakers(httpx2.Origin("https://a.example")) == (by_origin,)
    with pytest.raises(ValueError, match="absolute"):
        registry.breaker_for("/relative", failure_threshold=1, recovery_timeout=1.0)


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
    registry = BreakerRegistry(max_hosts=_SHARED_ORIGINS)
    outcomes: list[int] = []
    lock = threading.Lock()

    async def fetches(offset: int) -> None:
        ran = 0
        for n in range(_FETCHES):
            origin = _ORIGINS[(n + offset) % _SHARED_ORIGINS]
            breaker = registry.breaker_for(origin, failure_threshold=_THRESHOLD, recovery_timeout=_COOLDOWN)
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
    for breaker in registry:
        assert breaker.state is BreakerState.OPEN
        assert breaker.failure_count == _THRESHOLD, "a failure finishing after the trip changes nothing"
    assert sum(outcomes) < _THREADS * _FETCHES, "open breakers refused the rest"


class RegistryMachine(RuleBasedStateMachine):
    """Drive a small registry through lookups, trips, recoveries and time, checking its bounds."""

    def __init__(self) -> None:
        """Start with an empty registry on a hand-driven clock."""
        super().__init__()
        self.now = 0.0
        self.registry = BreakerRegistry(max_hosts=_MAX, clock=lambda: self.now)
        self.last_built: CircuitBreaker | None = None

    @rule(origin=st.sampled_from(_ORIGINS), threshold=st.sampled_from((1, _THRESHOLD)))
    def look_up(self, origin: str, threshold: int) -> None:
        """Fetch *origin*'s breaker, building it if new; the same key always returns the same breaker while held."""
        held = {(b.name, b.failure_threshold): b for b in self.registry}
        breaker = self.registry.breaker_for(origin, failure_threshold=threshold, recovery_timeout=_COOLDOWN)
        if (origin, threshold) in held:
            assert breaker is held[origin, threshold]
        assert self.registry.breakers()[-1] is breaker, "a lookup makes the breaker the most recently used"
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
        """Hold one breaker per origin and thresholds."""
        keys = [(b.name, b.failure_threshold, b.recovery_timeout) for b in self.registry]
        assert len(keys) == len(set(keys))

    @override
    def teardown(self) -> None:
        """Forget every breaker."""
        self.registry.clear()


TestRegistryMachine = RegistryMachine.TestCase
