# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""Snapshots of what a client reports, per scenario: its event sequence, and its OpenTelemetry span.

Both are contracts an observer or a dashboard is built on, so a change to either shows as a reviewed diff of
``__snapshots__/test_snapshots.ambr``. Call ids and durations differ from run to run and are excluded.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
from typing import TYPE_CHECKING

import anysqlite
import httpx2
import pytest
from hishel import AsyncSqliteStorage
from opentelemetry.metrics import NoOpMeterProvider
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from syrupy.filters import props

from headgate import Backoff, BreakerPolicy, Hedge, HttpCache, RateBudget, RetryPolicy, TransportPolicy, nothing
from headgate.errors import ClientError
from headgate.otel import OpenTelemetryObserver
from headgate.ratelimit import Rate
from headgate.retry import NO_RETRY
from headgate.testing import FakeTransport, TestClock, isolated_breakers
from tests.cache._helpers import FaultyStorage, every_get_policy
from tests.observe._support import Recorder, client

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

    from syrupy.assertion import SnapshotAssertion

    from headgate.events import Observer
    from tests.support.clients import StubClient

type _Scenario = Callable[[tuple[Observer, ...], AsyncSqliteStorage], Awaitable[None]]


def _half_jitter() -> float:
    """Return the middle of the jitter range, so each backoff wait is half its nominal value."""
    return 0.5


#: Send each call once, so a scenario's second call shows what the first left behind.
_ONCE = TransportPolicy(retry=NO_RETRY)

_DEPRECATED = {"Deprecation": "@1688169599", "Sunset": "Sun, 30 Jun 2024 23:59:59 GMT"}


@pytest.fixture
async def storage() -> AsyncIterator[AsyncSqliteStorage]:
    """Yield a hishel storage on an in-memory SQLite database, closed after the test."""
    store = AsyncSqliteStorage(connection=await anysqlite.connect(":memory:"))
    try:
        yield store
    finally:
        await store.close()


async def _call(built: StubClient, times: int = 1) -> None:
    """Make *times* ``GET x`` calls on *built*, swallowing the ``ClientError`` a scenario may end in."""
    for _ in range(times):
        with contextlib.suppress(ClientError):
            await built.get("x", expect=nothing)


async def _plain(observers: tuple[Observer, ...], _storage: AsyncSqliteStorage) -> None:
    """Make one call, answered at once."""
    await _call(client(FakeTransport().route("GET", "/v1/x"), *observers))


async def _retried(observers: tuple[Observer, ...], _storage: AsyncSqliteStorage) -> None:
    """Make one call, answered on its third attempt, backing off by a fixed jitter so the delays are reproducible."""
    policy = TransportPolicy(retry=RetryPolicy(backoff=Backoff(jitter=_half_jitter)))
    await _call(client(FakeTransport().route("GET", "/v1/x", 503, 502, 200), *observers, policy=policy))


async def _rate_limited(observers: tuple[Observer, ...], _storage: AsyncSqliteStorage) -> None:
    """Make one call the server rate-limits, then a second call that waits out the pause it put on the budget.

    The budget is real and runs on a test clock (the scenario is marked ``transport_policy``), so the pause shows as
    the first call's ``pushback`` and the second call's ``paused`` wait.
    """
    clock = TestClock()
    limited = httpx2.Response(429, headers={"Retry-After": "1"})
    budget = RateBudget(Rate.per_second(1000), timing=clock.timing, wall_clock=clock)
    with clock.install():
        built = client(FakeTransport().route("GET", "/v1/x", limited, 200), *observers, rate=budget, policy=_ONCE)
    await _call(built, times=2)


async def _hedged(observers: tuple[Observer, ...], _storage: AsyncSqliteStorage) -> None:
    """Make one call whose first send hangs, so a hedge goes out after ``after`` seconds and answers."""

    async def hangs(_request: httpx2.Request) -> httpx2.Response:
        await asyncio.Event().wait()
        raise AssertionError

    clock = TestClock()
    policy = TransportPolicy(retry=RetryPolicy(hedge=Hedge(after=0.5)))
    with clock.install():
        built = client(FakeTransport().route("GET", "/v1/x", hangs, 200), *observers, policy=policy)
    await _call(built)


async def _cache_hit(observers: tuple[Observer, ...], storage: AsyncSqliteStorage) -> None:
    """Make two calls for one cacheable resource, the second served from the cache."""
    fresh = httpx2.Response(200, content=b"cached", headers={"Cache-Control": "max-age=60"})
    policy = TransportPolicy(cache=HttpCache(policy=every_get_policy(), storage=storage))
    await _call(client(FakeTransport().route("GET", "/v1/x", fresh), *observers, policy=policy), times=2)


async def _cache_degraded(observers: tuple[Observer, ...], storage: AsyncSqliteStorage) -> None:
    """Make one call whose cache storage fails, so it is handed to the network."""
    broken = FaultyStorage(storage, OSError("disk gone"), failing="get_entries")
    policy = TransportPolicy(cache=HttpCache(policy=every_get_policy(), storage=broken))
    await _call(client(FakeTransport().route("GET", "/v1/x"), *observers, policy=policy))


async def _breaker_opening(observers: tuple[Observer, ...], _storage: AsyncSqliteStorage) -> None:
    """Fail once, opening the host's breaker, then make a call it refuses."""
    policy = TransportPolicy(retry=RetryPolicy(attempts=1), breaker=BreakerPolicy(failure_threshold=1))
    with isolated_breakers():
        built = client(FakeTransport().route("GET", "/v1/x", 503), *observers, policy=policy)
    await _call(built, times=2)


async def _deprecated(observers: tuple[Observer, ...], _storage: AsyncSqliteStorage) -> None:
    """Make one call to an endpoint announcing its deprecation and sunset."""
    await _call(client(FakeTransport().route("GET", "/v1/x", httpx2.Response(200, headers=_DEPRECATED)), *observers))


_SCENARIOS: dict[str, _Scenario] = {
    "plain": _plain,
    "retried": _retried,
    "rate_limited": _rate_limited,
    "hedged": _hedged,
    "cache_hit": _cache_hit,
    "cache_degraded": _cache_degraded,
    "breaker_opening": _breaker_opening,
    "deprecated": _deprecated,
}


#: The scenarios whose subject is the rate budget's own wait, which the suite otherwise makes a no-op.
_REAL_BUDGET = frozenset({"rate_limited"})

_PARAMS = [
    pytest.param(name, marks=pytest.mark.transport_policy) if name in _REAL_BUDGET else pytest.param(name)
    for name in sorted(_SCENARIOS)
]


@pytest.mark.parametrize("scenario", _PARAMS)
async def test_the_event_sequence_matches_the_snapshot(
    scenario: str, storage: AsyncSqliteStorage, snapshot: SnapshotAssertion
) -> None:
    """Check the scenario's events, in order, field by field, with ids and durations excluded."""
    recorder = Recorder()
    await _SCENARIOS[scenario]((recorder,), storage)
    sequence = [{"event": type(event).__name__, **dataclasses.asdict(event)} for event in recorder.events]
    assert sequence == snapshot(exclude=props("call_id", "elapsed"))


@pytest.mark.parametrize("scenario", _PARAMS)
async def test_the_spans_match_the_snapshot(
    scenario: str, storage: AsyncSqliteStorage, snapshot: SnapshotAssertion
) -> None:
    """Check the name, kind, status and attributes of each span the scenario's calls produce."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    observer = OpenTelemetryObserver(tracer_provider=provider, meter_provider=NoOpMeterProvider())
    await _SCENARIOS[scenario]((observer,), storage)
    provider.shutdown()
    spans = [
        {
            "name": span.name,
            "kind": span.kind.name,
            "status": span.status.status_code.name,
            "attributes": dict(span.attributes or {}),
        }
        for span in exporter.get_finished_spans()
    ]
    assert spans == snapshot
