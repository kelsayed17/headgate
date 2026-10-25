# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""What a call charges its origin's breaker with, the events its transitions emit, and the gate the retry layer reads.

* a 5xx that survived retries and a network failure are failures; a 429 is not (the origin is up, and asked the rate
  budget to slow down), nor is a failure of our own side's making (a request httpx2 refused to write, a scheme it
  cannot speak, our pool running dry), nor a 5xx served from the cache, which tested nothing;
* every transition (closed to open, open to half-open, half-open to closed or open) is a ``BreakerChanged`` event;
* every call carries ``BREAKER_GATE``, true once its origin's breaker refuses, except for the probe itself.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, override

import anysqlite
import httpx2
import pytest
from hishel import AsyncSqliteStorage, FilterPolicy

from headgate import BreakerPolicy, BreakerRegistry, HttpCache, HttpOptions, TransportPolicy
from headgate._cache import StatusCodeFilter
from headgate._core import BREAKER_GATE, BreakerChanged, BreakerState, CallContext, Event
from headgate.errors import ClientError, RateLimitError, UpstreamStatusError
from headgate.ratelimit import Rate
from headgate.retry import NO_RETRY
from tests.support.breakers import failures
from tests.support.clients import StubClient
from tests.support.clock import FakeClock
from tests.support.responses import RESPONSE

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

_HOST = "charged.example"
_ORIGIN = f"https://{_HOST}"
_RATE = Rate.per_second(1000)

#: The breakers here trip on the second failure and stay open for a minute.
_THRESHOLD = 2
_COOLDOWN = 60.0


class _Recorder:
    """Collect every event delivered."""

    def __init__(self) -> None:
        """Start empty."""
        self.events: list[Event] = []

    def notify(self, event: Event) -> None:
        """Keep *event*."""
        self.events.append(event)

    def transitions(self) -> list[tuple[BreakerState, BreakerState, int]]:
        """Return each ``BreakerChanged`` event's previous state, new state and failure count."""
        return [(e.previous, e.state, e.failures) for e in self.events if isinstance(e, BreakerChanged)]


def _client(
    handler: Callable[[httpx2.Request], httpx2.Response],
    *,
    policy: TransportPolicy | None = None,
    recorder: _Recorder | None = None,
) -> StubClient:
    """Return a client against *handler*, sending each call once, on the default registry's default breaker."""
    return StubClient(
        base_url=_ORIGIN,
        rate=_RATE,
        policy=policy or TransportPolicy(retry=NO_RETRY),
        http=HttpOptions(transport=httpx2.MockTransport(handler), observers=() if recorder is None else (recorder,)),
    )


def _raising(exc: type[httpx2.TransportError]) -> Callable[[httpx2.Request], httpx2.Response]:
    """Return a handler that raises *exc* for every request."""

    def handler(request: httpx2.Request) -> httpx2.Response:
        msg = "simulated"
        raise exc(msg, request=request)

    return handler


# --- What is charged ---


async def test_a_429_is_not_a_host_failure() -> None:
    """Charge nothing for a 429 that survived retries: it is a rate-limit signal, not an outage."""
    client = _client(lambda _request: httpx2.Response(429))
    with pytest.raises(RateLimitError):
        await client.get("x", expect=RESPONSE)
    assert failures(_HOST) == 0


async def test_a_5xx_is_a_host_failure() -> None:
    """Charge a 5xx that survived retries."""
    client = _client(lambda _request: httpx2.Response(502))
    with pytest.raises(UpstreamStatusError):
        await client.get("x", expect=RESPONSE)
    assert failures(_HOST) == 1


@pytest.mark.parametrize("exc", [httpx2.LocalProtocolError, httpx2.UnsupportedProtocol, httpx2.PoolTimeout])
async def test_a_local_failure_is_not_a_host_failure(exc: type[httpx2.TransportError]) -> None:
    """Charge nothing for a failure our own side made: the origin never had a say."""
    with pytest.raises(ClientError):
        await _client(_raising(exc)).get("x", expect=RESPONSE)
    assert failures(_HOST) == 0


@pytest.mark.parametrize("exc", [httpx2.ConnectError, httpx2.ReadTimeout, httpx2.RemoteProtocolError])
async def test_a_network_failure_is_a_host_failure(exc: type[httpx2.TransportError]) -> None:
    """Charge a failure the origin's side of the wire produced."""
    with pytest.raises(ClientError):
        await _client(_raising(exc)).get("x", expect=RESPONSE)
    assert failures(_HOST) == 1


@pytest.fixture
async def storage() -> AsyncIterator[AsyncSqliteStorage]:
    """Yield an in-memory hishel storage, closed after the test."""
    store = AsyncSqliteStorage(connection=await anysqlite.connect(":memory:"))
    try:
        yield store
    finally:
        await store.close()


async def test_a_5xx_served_from_the_cache_is_not_a_host_failure(storage: AsyncSqliteStorage) -> None:
    """Charge the 503 the network returned once, not the copies the cache serves afterwards."""
    served: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        served.append(request)
        return httpx2.Response(503, headers={"Cache-Control": "max-age=3600"})

    policy = FilterPolicy(response_filters=[StatusCodeFilter(frozenset({503}))])
    client = _client(handler, policy=TransportPolicy(retry=NO_RETRY, cache=HttpCache(policy=policy, storage=storage)))
    for _ in range(_THRESHOLD + 1):
        with pytest.raises(UpstreamStatusError):
            await client.get("x", expect=RESPONSE)
    assert len(served) == 1, "precondition: the later calls were served from the cache"
    assert failures(_HOST) == 1


# --- Transitions as events ---


async def test_every_transition_is_reported_once_by_the_call_that_caused_it() -> None:
    """Emit closed to open, open to half-open and half-open to open, then half-open to closed, each with its call."""
    clock = FakeClock()
    registry = BreakerRegistry(clock=clock)
    breaker = BreakerPolicy(failure_threshold=_THRESHOLD, recovery_timeout=_COOLDOWN, registry=registry)
    statuses = iter([503, 503, 503, 200])
    recorder = _Recorder()
    client = _client(
        lambda _request: httpx2.Response(next(statuses), content=b"{}"),
        policy=TransportPolicy(retry=NO_RETRY, breaker=breaker),
        recorder=recorder,
    )

    for _ in range(_THRESHOLD):
        with pytest.raises(UpstreamStatusError):
            await client.get("x", expect=RESPONSE)
    clock.advance(_COOLDOWN)
    with pytest.raises(UpstreamStatusError):
        await client.get("x", expect=RESPONSE)  # the probe fails
    clock.advance(_COOLDOWN)
    await client.get("x", expect=RESPONSE)  # the probe succeeds

    closed, opened, probing = BreakerState.CLOSED, BreakerState.OPEN, BreakerState.HALF_OPEN
    assert recorder.transitions() == [
        (closed, opened, _THRESHOLD),
        (opened, probing, _THRESHOLD),
        (probing, opened, _THRESHOLD),
        (opened, probing, _THRESHOLD),
        (probing, closed, 0),
    ]
    changes = [event for event in recorder.events if isinstance(event, BreakerChanged)]
    assert {event.origin for event in changes} == {_ORIGIN}
    assert all(event.call is not None and event.call.url == "x" for event in changes)


async def test_a_client_without_a_breaker_reports_no_transition() -> None:
    """Emit no ``BreakerChanged`` from a client built with ``breaker=None``, observers or not."""
    recorder = _Recorder()
    client = _client(
        lambda _request: httpx2.Response(503), policy=TransportPolicy(retry=NO_RETRY, breaker=None), recorder=recorder
    )
    for _ in range(_THRESHOLD):
        with pytest.raises(UpstreamStatusError):
            await client.get("x", expect=RESPONSE)
    assert recorder.transitions() == []


# --- The gate the retry layer reads ---


class _GateReader(httpx2.AsyncBaseTransport):
    """Record the call's ``BREAKER_GATE`` reading as each request reaches the wire, then answer *status*."""

    def __init__(self, status: int, before: Callable[[], None] | None = None) -> None:
        """Answer *status*, after running *before* (which may change the breaker) if given."""
        self._status = status
        self._before = before
        self.readings: list[bool | None] = []

    @override
    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        if self._before is not None:
            self._before()
        gate = CallContext.of(request).get(BREAKER_GATE)
        self.readings.append(None if gate is None else gate())
        return httpx2.Response(self._status, content=b"{}")


def _gated_client(wire: _GateReader, policy: TransportPolicy) -> StubClient:
    """Return a client over *wire* with *policy*."""
    return StubClient(base_url=_ORIGIN, rate=_RATE, policy=policy, http=HttpOptions(transport=wire))


async def test_the_gate_opens_when_another_call_trips_the_breaker_mid_call() -> None:
    """Read false while the breaker admits, and true once a concurrent failure has opened it."""
    clock = FakeClock()
    registry = BreakerRegistry(clock=clock)
    breaker = BreakerPolicy(failure_threshold=1, recovery_timeout=_COOLDOWN, registry=registry)

    def trip() -> None:
        tripped = registry.breaker_for(_ORIGIN, failure_threshold=1, recovery_timeout=_COOLDOWN)
        with contextlib.suppress(RuntimeError), tripped.track() as verdict:
            verdict.mark_failed()
            raise RuntimeError

    healthy = _GateReader(200)
    await _gated_client(healthy, TransportPolicy(retry=NO_RETRY, breaker=breaker)).get("x", expect=RESPONSE)
    tripping = _GateReader(200, before=trip)
    await _gated_client(tripping, TransportPolicy(retry=NO_RETRY, breaker=breaker)).get("x", expect=RESPONSE)

    assert (healthy.readings, tripping.readings) == ([False], [True])


async def test_the_probe_is_not_stopped_by_its_own_probe_slot() -> None:
    """Read false for the half-open breaker's probe, so its own retries are its one recovery test."""
    clock = FakeClock()
    registry = BreakerRegistry(clock=clock)
    breaker = BreakerPolicy(failure_threshold=1, recovery_timeout=_COOLDOWN, registry=registry)
    failing = _gated_client(_GateReader(503), TransportPolicy(retry=NO_RETRY, breaker=breaker))
    with pytest.raises(UpstreamStatusError):
        await failing.get("x", expect=RESPONSE)
    clock.advance(_COOLDOWN)

    probe = _GateReader(200)
    await _gated_client(probe, TransportPolicy(retry=NO_RETRY, breaker=breaker)).get("x", expect=RESPONSE)

    assert probe.readings == [False]


async def test_a_client_without_a_breaker_has_a_gate_that_never_closes() -> None:
    """Set a gate that always reads false on a client with ``breaker=None``."""
    wire = _GateReader(200)
    await _gated_client(wire, TransportPolicy(retry=NO_RETRY, breaker=None)).get("x", expect=RESPONSE)
    assert wire.readings == [False]
