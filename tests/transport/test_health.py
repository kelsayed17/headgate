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
from hishel import AsyncSqliteStorage, Entry, FilterPolicy

from headgate._breaker import HOST_BREAKERS, CircuitBreaker
from headgate._cache import (
    CACHE_DEGRADATIONS,
    CACHE_DEGRADED_EXT,
    MethodFilter,
    ResilientCacheTransport,
    StatusCodeFilter,
)
from headgate.health import BreakerState, breaker_states, cache_degradations

if TYPE_CHECKING:
    from collections.abc import Iterator

_HOST = "health.example.test"

#: The registry's breakers trip after this many consecutive failures.
_DEFAULT_THRESHOLD = 5

#: How many requests the cache-fault test sends.
_REQUESTS = 2


@pytest.fixture(autouse=True)
def _clean_registries() -> Iterator[None]:
    """Drop the test host from both process-global registries around each test."""
    HOST_BREAKERS.pop(_HOST, None)
    CACHE_DEGRADATIONS.pop(_HOST, None)
    yield
    HOST_BREAKERS.pop(_HOST, None)
    CACHE_DEGRADATIONS.pop(_HOST, None)


def _fail_once(breaker: CircuitBreaker) -> None:
    """Run one tracked fetch that marks a wire failure and raises."""
    with breaker.track() as verdict:
        verdict.mark_failed()
        raise ConnectionError


def _trip(breaker: CircuitBreaker) -> None:
    """Record the default threshold's worth of marked failures."""
    for _ in range(_DEFAULT_THRESHOLD):
        with pytest.raises(ConnectionError):
            _fail_once(breaker)


def test_breaker_states_reports_a_tripped_host_as_open() -> None:
    """Report a host's breaker as closed and admitting, then open and refusing once it trips."""
    breaker = HOST_BREAKERS[_HOST]
    assert breaker_states()[_HOST] == (BreakerState.CLOSED, False)

    _trip(breaker)

    assert breaker_states()[_HOST] == (BreakerState.OPEN, True)


def test_a_tripped_breaker_past_its_cooldown_is_open_but_not_refusing() -> None:
    """Report open but not refusing: ``state`` waits for a probe, ``refusing`` follows the cooldown."""
    breaker = CircuitBreaker(name=_HOST, recovery_timeout=0.0)
    HOST_BREAKERS[_HOST] = breaker
    _trip(breaker)

    assert breaker_states()[_HOST] == (BreakerState.OPEN, False)


def test_breaker_states_is_a_copy() -> None:
    """Return a fresh mapping each call, so clearing a snapshot leaves the registry intact."""
    HOST_BREAKERS[_HOST]  # the registry is a defaultdict: reading the host builds its breaker
    snapshot = breaker_states()
    snapshot.clear()
    assert _HOST in breaker_states()


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
    transport = ResilientCacheTransport(
        next_transport=_Network(),
        storage=storage,
        policy=FilterPolicy(
            request_filters=[MethodFilter(frozenset({"GET"}))],
            response_filters=[StatusCodeFilter(frozenset({200}))],
        ),
        max_body_bytes=1024 * 1024,
    )

    try:
        for _ in range(_REQUESTS):
            response = await transport.handle_async_request(httpx2.Request("GET", f"https://{_HOST}/x?api_key=secret"))
            assert response.extensions[CACHE_DEGRADED_EXT]
    finally:
        await storage.close()

    assert cache_degradations()[_HOST] == _REQUESTS
