"""``headgate.health`` — the read surface a composition root exports transport degradation through.

The tier neither logs nor emits metrics, so these snapshots are the only way an open breaker or a broken
cache reaches a dashboard. Each test drives the real mechanism (a breaker tripped by failures, a cache
storage that faults) rather than writing the registry directly, so a rename on either side breaks here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, override

import anysqlite
import httpx2
import pytest
from hishel import AsyncSqliteStorage, Entry, FilterPolicy

from headgate._async_breaker import AsyncCircuitBreaker, _host_breakers
from headgate._cache import (
    MethodFilter,
    ResilientCacheTransport,
    StatusCodeFilter,
    _cache_degradations,
    _NoCloseStorage,
)
from headgate.health import BreakerState, breaker_states, cache_degradations

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

_HOST = "health.example.test"


@pytest.fixture(autouse=True)
def _clean_registries() -> Iterator[None]:
    _host_breakers.pop(_HOST, None)
    _cache_degradations.pop(_HOST, None)
    yield
    _host_breakers.pop(_HOST, None)
    _cache_degradations.pop(_HOST, None)


def test_breaker_states_reports_a_tripped_host_as_open() -> None:
    breaker = _host_breakers[_HOST]
    assert breaker_states()[_HOST] == (BreakerState.CLOSED, False)

    _trip(breaker)

    assert breaker_states()[_HOST] == (BreakerState.OPEN, True)


def test_a_tripped_breaker_past_its_cooldown_is_open_but_not_refusing() -> None:
    """The latch ``state`` alone would show: OPEN stays OPEN until some call takes the probe."""
    breaker = AsyncCircuitBreaker(name=_HOST, recovery_timeout=0.0)
    _host_breakers[_HOST] = breaker
    _trip(breaker)

    assert breaker_states()[_HOST] == (BreakerState.OPEN, False)


def _trip(breaker: AsyncCircuitBreaker) -> None:
    for _ in range(5):
        with pytest.raises(OSError), breaker.track() as verdict:
            verdict.host_failed()
            raise OSError


def test_breaker_states_is_a_copy() -> None:
    _host_breakers[_HOST]
    snapshot = breaker_states()
    snapshot.clear()
    assert _HOST in breaker_states()


@pytest.fixture
async def storage() -> AsyncIterator[AsyncSqliteStorage]:
    conn = await anysqlite.connect(":memory:")
    store = AsyncSqliteStorage(connection=conn)
    try:
        yield store
    finally:
        await store.close()


class _LookupFails(_NoCloseStorage):
    @override
    async def get_entries(self, key: str) -> list[Entry]:
        raise ValueError("corrupt cache entry")


class _Network(httpx2.AsyncBaseTransport):
    @override
    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, text="from-network")


async def test_a_cache_fault_counts_one_degradation_for_its_host(storage: AsyncSqliteStorage) -> None:
    transport = ResilientCacheTransport(
        next_transport=_Network(),
        storage=_LookupFails(storage),
        policy=FilterPolicy(
            request_filters=[MethodFilter(frozenset({"GET"}))],
            response_filters=[StatusCodeFilter(frozenset({200}))],
        ),
        max_body_bytes=1024 * 1024,
    )

    for _ in range(2):
        response = await transport.handle_async_request(httpx2.Request("GET", f"https://{_HOST}/x?api_key=secret"))
        assert response.extensions["headgate_cache_degraded"]

    assert cache_degradations()[_HOST] == 2
