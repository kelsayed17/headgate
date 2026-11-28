# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""``headgate.health``: the read surface an application exports transport degradation through.

headgate neither logs nor emits metrics, so these snapshots are the only way an open breaker or a broken
cache reaches a dashboard. Each test drives the real mechanism (a breaker tripped by failures, a cache
storage that faults) rather than writing the registry directly, so a rename on either side breaks here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, override

import anysqlite
import httpx2
import pytest
from hishel import AsyncSqliteStorage, Entry

from headgate import BreakerPolicy, HttpCache
from headgate._core import OBSERVER_FAILURES
from headgate.breaker import BreakerRegistry, BreakerState
from headgate.breaker._registry import table_of
from headgate.cache import _transport
from headgate.cache._transport import CACHE_DEGRADATIONS, CACHE_DEGRADED_EXT, ResilientCacheTransport
from headgate.health import BreakerSnapshot, breaker_states, cache_degradations, observer_failures

if TYPE_CHECKING:
    from collections.abc import Iterator

    from headgate.breaker._breaker import CircuitBreaker

_HOST = "health.example.test"
_ORIGIN = f"https://{_HOST}"

#: The test breakers trip after this many consecutive failures, and one cooldown no test outlasts.
_THRESHOLD = 5
_LONG_COOLDOWN = 3600.0

#: How many requests the cache-fault test sends.
_REQUESTS = 2


@pytest.fixture(autouse=True)
def _clean_cache_counts() -> Iterator[None]:
    """Drop the test host from the process-global degradation count around each test."""
    CACHE_DEGRADATIONS.pop(_HOST, None)
    yield
    CACHE_DEGRADATIONS.pop(_HOST, None)


def _fail_once(breaker: CircuitBreaker) -> None:
    """Run one tracked fetch that marks a wire failure and raises."""
    with breaker.track() as verdict:
        verdict.mark_failed()
        raise ConnectionError


def _trip(breaker: CircuitBreaker) -> None:
    """Record the threshold's worth of marked failures."""
    for _ in range(_THRESHOLD):
        with pytest.raises(ConnectionError):
            _fail_once(breaker)


def test_breaker_states_reports_a_tripped_host_as_open() -> None:
    """Report a host's breaker as closed and admitting, then open and refusing once it trips."""
    registry = BreakerRegistry(BreakerPolicy(failure_threshold=_THRESHOLD, recovery_timeout=_LONG_COOLDOWN))
    breaker = table_of(registry).breaker_for(_ORIGIN)
    assert breaker_states(registry)[_ORIGIN] == BreakerSnapshot(state=BreakerState.CLOSED, refusing=False, failures=0)

    _trip(breaker)

    assert breaker_states(registry)[_ORIGIN] == BreakerSnapshot(
        state=BreakerState.OPEN, refusing=True, failures=_THRESHOLD
    )


def test_a_tripped_breaker_past_its_cooldown_is_open_but_not_refusing() -> None:
    """Report open but not refusing: ``state`` waits for a probe, ``refusing`` follows the cooldown."""
    registry = BreakerRegistry(BreakerPolicy(failure_threshold=_THRESHOLD, recovery_timeout=0.0))
    _trip(table_of(registry).breaker_for(_ORIGIN))

    assert breaker_states(registry)[_ORIGIN] == BreakerSnapshot(
        state=BreakerState.OPEN, refusing=False, failures=_THRESHOLD
    )


def test_a_snapshot_counts_failures() -> None:
    """Report the consecutive failures beside the state, in a snapshot that is a value, not a tuple."""
    registry = BreakerRegistry(BreakerPolicy(failure_threshold=3, recovery_timeout=_LONG_COOLDOWN))
    breaker = table_of(registry).breaker_for(_ORIGIN)
    for _ in range(2):
        with pytest.raises(ConnectionError):
            _fail_once(breaker)

    snapshot = breaker_states(registry)[_ORIGIN]

    assert snapshot == BreakerSnapshot(state=BreakerState.CLOSED, refusing=False, failures=2)
    assert not isinstance(snapshot, tuple)


@pytest.mark.parametrize("name", ["state", "refusing", "failures"])
def test_a_snapshot_cannot_be_changed(name: str) -> None:
    """Refuse to rebind any field of a snapshot: it is what the breaker was at one instant."""
    snapshot = BreakerSnapshot(state=BreakerState.OPEN, refusing=True, failures=5)
    with pytest.raises(AttributeError):
        setattr(snapshot, name, None)


def test_breaker_states_is_a_copy() -> None:
    """Return a fresh mapping each call, so clearing a snapshot leaves the registry intact."""
    registry = BreakerRegistry()
    table_of(registry).breaker_for(_ORIGIN)
    breaker_states(registry).clear()
    assert _ORIGIN in breaker_states(registry)


def test_observer_failures_is_a_copy() -> None:
    """Return a fresh mapping each call, so clearing a snapshot leaves the count intact."""
    snapshot = observer_failures()
    snapshot.clear()
    assert observer_failures() == dict(OBSERVER_FAILURES)


class _LookupFails(AsyncSqliteStorage):
    """A storage whose every lookup fails."""

    @override
    async def get_entries(self, key: str) -> list[Entry]:
        msg = "corrupt cache entry"
        raise ValueError(msg)


class _Network(httpx2.AsyncBaseTransport):
    """A network that answers every request with a 200."""

    @override
    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, text="from-network")


async def test_a_cache_fault_counts_one_degradation_for_its_host() -> None:
    """Count one degradation per request that found the cache broken, keyed by host."""
    storage = _LookupFails(connection=await anysqlite.connect(":memory:"))
    transport = ResilientCacheTransport(_Network(), HttpCache(storage=storage), max_body_bytes=1024 * 1024)

    try:
        for _ in range(_REQUESTS):
            response = await transport.handle_async_request(httpx2.Request("GET", f"https://{_HOST}/x?api_key=secret"))
            assert response.extensions[CACHE_DEGRADED_EXT]
    finally:
        await storage.close()

    assert cache_degradations()[_HOST] == _REQUESTS


@pytest.fixture
def _no_other_counts() -> Iterator[None]:
    """Empty the process-global degradation count for one test, and put back what other tests counted."""
    held = dict(CACHE_DEGRADATIONS)
    CACHE_DEGRADATIONS.clear()
    yield
    CACHE_DEGRADATIONS.clear()
    CACHE_DEGRADATIONS.update(held)


@pytest.mark.usefixtures("_no_other_counts")
async def test_the_count_keeps_only_the_most_recently_degraded_hosts(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hold at most the bound's worth of hosts, dropping the one least recently degraded to make room."""
    monkeypatch.setattr(_transport, "DEFAULT_MAX_HOSTS", _REQUESTS)
    storage = _LookupFails(connection=await anysqlite.connect(":memory:"))
    transport = ResilientCacheTransport(_Network(), HttpCache(storage=storage), max_body_bytes=1024 * 1024)

    try:
        for host in ("first.test", "second.test", "first.test", "third.test"):
            await transport.handle_async_request(httpx2.Request("GET", f"https://{host}/x"))
    finally:
        await storage.close()

    assert cache_degradations() == {"first.test": 2, "third.test": 1}, "second.test was degraded least recently"
