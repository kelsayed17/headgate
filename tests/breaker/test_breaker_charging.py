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
import dataclasses
from typing import TYPE_CHECKING, ClassVar, override

import httpx2
import pytest

from headgate import (
    Auth,
    BreakerPolicy,
    CallPolicy,
    Client,
    ClientError,
    HttpOptions,
    Middleware,
    Next,
    Rate,
    RateLimitError,
    RetryPolicy,
    SigningAuth,
    TransportError,
    UpstreamStatusError,
)
from headgate._core import BREAKER_GATE, BreakerChanged, BreakerState, CallContext
from headgate.breaker import BreakerRegistry
from headgate.breaker._registry import table_of
from headgate.errors import CircuitOpenError
from headgate.retry import Backoff
from headgate.testing import FakeTransport, TestClock, fake_network
from headgate.timing import Timing
from tests.support.breakers import failures
from tests.support.events import Recorder
from tests.support.responses import RESPONSE
from tests.support.timing import built_on

if TYPE_CHECKING:
    from collections.abc import Callable

_HOST = "charged.example"
_ORIGIN = f"https://{_HOST}"
_RATE = Rate.per_second(1000)

#: The breakers here trip on the second failure and stay open for a minute.
_THRESHOLD = 2
_COOLDOWN = 60.0


def _transitions(recorder: Recorder) -> list[tuple[BreakerState, BreakerState, int]]:
    """Return each ``BreakerChanged`` event's previous state, new state and failure count."""
    return [(event.previous, event.state, event.failures) for event in recorder.of(BreakerChanged)]


def _client(
    handler: Callable[[httpx2.Request], httpx2.Response],
    *,
    policy: CallPolicy | None = None,
    recorder: Recorder | None = None,
    clock: TestClock | None = None,
) -> Client:
    """Return a client against *handler*, sending each call once, on a default breaker of its own, on *clock*."""
    with clock.install() if clock is not None else contextlib.nullcontext():
        return Client(
            base_url=_ORIGIN,
            policy=dataclasses.replace(policy or CallPolicy(retry=None), rate=_RATE),
            http=HttpOptions(transport=httpx2.MockTransport(handler)),
            observers=() if recorder is None else (recorder,),
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
    assert failures(client, _HOST) == 0


async def test_a_5xx_is_a_host_failure() -> None:
    """Charge a 5xx that survived retries."""
    client = _client(lambda _request: httpx2.Response(502))
    with pytest.raises(UpstreamStatusError):
        await client.get("x", expect=RESPONSE)
    assert failures(client, _HOST) == 1


@pytest.mark.parametrize("exc", [httpx2.LocalProtocolError, httpx2.UnsupportedProtocol, httpx2.PoolTimeout])
async def test_a_local_failure_is_not_a_host_failure(exc: type[httpx2.TransportError]) -> None:
    """Charge nothing for a failure our own side made: the origin never had a say."""
    client = _client(_raising(exc))
    with pytest.raises(ClientError):
        await client.get("x", expect=RESPONSE)
    assert failures(client, _HOST) == 0


@pytest.mark.parametrize("exc", [httpx2.ConnectError, httpx2.ReadTimeout, httpx2.RemoteProtocolError])
async def test_a_network_failure_is_a_host_failure(exc: type[httpx2.TransportError]) -> None:
    """Charge a failure the origin's side of the wire produced."""
    client = _client(_raising(exc))
    with pytest.raises(ClientError):
        await client.get("x", expect=RESPONSE)
    assert failures(client, _HOST) == 1


@pytest.mark.call_policy
@pytest.mark.parametrize(
    "handler",
    [_raising(httpx2.ConnectError), lambda _request: httpx2.Response(503)],
    ids=["network-failure", "5xx"],
)
async def test_a_failure_is_charged_though_the_rate_budget_withdrew_the_retry(
    handler: Callable[[httpx2.Request], httpx2.Response],
) -> None:
    """Charge the attempt that reached the network when the retry after it never went out.

    The retry's turn on the rate budget comes after the call's deadline, so the budget withdraws it, unsent, and the
    call ends with the first attempt's failure: that attempt is the one the breaker is charged for. The budget sees
    the turn is too late at once, on the real clock, without waiting for it.
    """
    fake = FakeTransport().reply("GET", "/x", handler)
    with fake_network(fake):
        client = Client(
            base_url=_ORIGIN,
            policy=CallPolicy(
                rate=Rate.per_second(1), retry=RetryPolicy(attempts=2, backoff=Backoff(factor=0.01, max_wait=0.01))
            ),
        )
    with pytest.raises(ClientError) as raised:
        await client.get("x", expect=RESPONSE, deadline=0.5)

    assert raised.value.__notes__[-1] == "deadline exhausted"
    assert len(fake.calls) == 1
    assert failures(client, _HOST) == 1


class _Offline:
    """A middleware that fails each attempt itself, as an offline mode does, so no attempt reaches the network."""

    credential_headers = frozenset[str]()
    credential_params = frozenset[str]()

    async def __call__(self, request: httpx2.Request, call_next: Next) -> httpx2.Response:
        """Raise the failure a network transport would, without sending."""
        del call_next
        msg = "offline mode"
        raise httpx2.ConnectError(msg, request=request)


class _KeylessSigner(SigningAuth):
    """A signer whose key file is missing, so each attempt fails before it is sent."""

    credential_headers: ClassVar = frozenset({"X-Signature"})

    @override
    def sign(self, request: httpx2.Request, now: float) -> None:
        missing = "signing.pem"
        raise FileNotFoundError(missing)


@pytest.mark.parametrize(
    ("auth", "middleware"), [(None, (_Offline(),)), (_KeylessSigner(), ())], ids=["middleware", "signer"]
)
async def test_a_failure_before_the_network_is_not_a_host_failure(
    auth: Auth | None, middleware: tuple[Middleware, ...]
) -> None:
    """Charge nothing for a network failure a middleware or a signer raised: no attempt reached the network."""
    sent: list[httpx2.Request] = []
    client = Client(
        base_url=_ORIGIN,
        auth=auth,
        policy=CallPolicy(rate=_RATE, retry=RetryPolicy(attempts=2), breaker=BreakerPolicy(failure_threshold=1)),
        http=HttpOptions(transport=httpx2.MockTransport(lambda request: sent.append(request) or httpx2.Response(200))),
        middleware=middleware,
    )
    for _ in range(2):
        with pytest.raises(TransportError) as caught:
            await client.get("x", expect=RESPONSE)
        assert not isinstance(caught.value, CircuitOpenError), "the breaker opened on a failure that sent nothing"

    assert sent == []
    assert failures(client, _HOST) == 0


# --- Transitions as events ---


async def test_every_transition_is_reported_once_by_the_call_that_caused_it() -> None:
    """Emit closed to open, open to half-open and half-open to open, then half-open to closed, each with its call."""
    clock = TestClock()
    breaker = built_on(
        Timing(clock=clock),
        lambda: BreakerRegistry(BreakerPolicy(failure_threshold=_THRESHOLD, recovery_timeout=_COOLDOWN)),
    )
    statuses = iter([503, 503, 503, 200])
    recorder = Recorder()
    client = _client(
        lambda _request: httpx2.Response(next(statuses), content=b"{}"),
        policy=CallPolicy(retry=None, breaker=breaker),
        recorder=recorder,
        clock=clock,
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
    assert _transitions(recorder) == [
        (closed, opened, _THRESHOLD),
        (opened, probing, _THRESHOLD),
        (probing, opened, _THRESHOLD + 1),
        (opened, probing, _THRESHOLD + 1),
        (probing, closed, 0),
    ]
    changes = [event for event in recorder.events if isinstance(event, BreakerChanged)]
    assert {event.origin for event in changes} == {_ORIGIN}
    assert all(event.call is not None and event.call.url == "x" for event in changes)


async def test_a_client_without_a_breaker_reports_no_transition() -> None:
    """Emit no ``BreakerChanged`` from a client built with ``breaker=None``, observers or not."""
    recorder = Recorder()
    client = _client(
        lambda _request: httpx2.Response(503), policy=CallPolicy(retry=None, breaker=None), recorder=recorder
    )
    for _ in range(_THRESHOLD):
        with pytest.raises(UpstreamStatusError):
            await client.get("x", expect=RESPONSE)
    assert _transitions(recorder) == []


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


def _gated_client(wire: _GateReader, policy: CallPolicy, clock: TestClock | None = None) -> Client:
    """Return a client over *wire* with *policy*, built on *clock* (as its breaker registry is) when given one."""
    with clock.install() if clock is not None else contextlib.nullcontext():
        return Client(
            base_url=_ORIGIN, policy=dataclasses.replace(policy, rate=_RATE), http=HttpOptions(transport=wire)
        )


async def test_the_gate_opens_when_another_call_trips_the_breaker_mid_call() -> None:
    """Read false while the breaker admits, and true once a concurrent failure has opened it."""
    clock = TestClock()
    breaker = registry = built_on(
        Timing(clock=clock), lambda: BreakerRegistry(BreakerPolicy(failure_threshold=1, recovery_timeout=_COOLDOWN))
    )

    def trip() -> None:
        tripped = table_of(registry).breaker_for(_ORIGIN)
        with contextlib.suppress(RuntimeError), tripped.track() as verdict:
            verdict.mark_failed()
            raise RuntimeError

    healthy = _GateReader(200)
    await _gated_client(healthy, CallPolicy(retry=None, breaker=breaker), clock).get("x", expect=RESPONSE)
    tripping = _GateReader(200, before=trip)
    await _gated_client(tripping, CallPolicy(retry=None, breaker=breaker), clock).get("x", expect=RESPONSE)

    assert (healthy.readings, tripping.readings) == ([False], [True])


async def test_the_gate_stays_shut_for_a_call_from_before_the_trip() -> None:
    """Read true for a call admitted before the breaker opened, even once a probe would be admitted.

    Its retries would otherwise reach the recovering origin beside the probe.
    """
    clock = TestClock()
    breaker = registry = built_on(
        Timing(clock=clock), lambda: BreakerRegistry(BreakerPolicy(failure_threshold=1, recovery_timeout=_COOLDOWN))
    )

    def trip_and_cool_down() -> None:
        tripped = table_of(registry).breaker_for(_ORIGIN)
        with contextlib.suppress(RuntimeError), tripped.track() as verdict:
            verdict.mark_failed()
            raise RuntimeError
        clock.advance(_COOLDOWN)

    stale = _GateReader(200, before=trip_and_cool_down)
    await _gated_client(stale, CallPolicy(retry=None, breaker=breaker), clock).get("x", expect=RESPONSE)

    assert stale.readings == [True]


async def test_the_probe_is_not_stopped_by_its_own_probe_slot() -> None:
    """Read false for the half-open breaker's probe, so its own retries are its one recovery test."""
    clock = TestClock()
    breaker = built_on(
        Timing(clock=clock), lambda: BreakerRegistry(BreakerPolicy(failure_threshold=1, recovery_timeout=_COOLDOWN))
    )
    failing = _gated_client(_GateReader(503), CallPolicy(retry=None, breaker=breaker), clock)
    with pytest.raises(UpstreamStatusError):
        await failing.get("x", expect=RESPONSE)
    clock.advance(_COOLDOWN)

    probe = _GateReader(200)
    await _gated_client(probe, CallPolicy(retry=None, breaker=breaker), clock).get("x", expect=RESPONSE)

    assert probe.readings == [False]


async def test_a_client_without_a_breaker_has_a_gate_that_never_closes() -> None:
    """Set a gate that always reads false on a client with ``breaker=None``."""
    wire = _GateReader(200)
    await _gated_client(wire, CallPolicy(retry=None, breaker=None)).get("x", expect=RESPONSE)
    assert wire.readings == [False]
