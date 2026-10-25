# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""``isolated_breakers``: the clients built in a block keep their breakers in a registry of their own."""

from __future__ import annotations

import pytest

from headgate import BreakerPolicy, BreakerRegistry, HttpOptions, Rate, TransportPolicy, nothing
from headgate.breaker import DEFAULT_REGISTRY, BreakerState
from headgate.errors import CircuitOpenError, UpstreamStatusError
from headgate.health import breaker_states
from headgate.retry import NO_RETRY
from headgate.testing import FakeTransport, TestClock, isolated_breakers
from tests.support.clients import StubClient

_HOST = "flaky.test"
_COOLDOWN = 30.0
#: One failure opens the breaker.
_TRIP_AT_ONCE = BreakerPolicy(failure_threshold=1, recovery_timeout=_COOLDOWN)


def _client(fake: FakeTransport, breaker: BreakerPolicy = _TRIP_AT_ONCE) -> StubClient:
    """Build a client for :data:`_HOST` that sends once and trips its breaker on the first failure."""
    return StubClient(
        base_url=f"https://{_HOST}",
        rate=Rate.per_second(1000),
        policy=TransportPolicy(retry=NO_RETRY, breaker=breaker),
        http=HttpOptions(transport=fake),
    )


async def test_failures_open_a_breaker_only_in_the_isolated_registry() -> None:
    """Open the host's breaker in the yielded registry and leave the default registry untouched."""
    fake = FakeTransport().route("GET", "/x", 503)
    with isolated_breakers() as registry:
        client = _client(fake)
    with pytest.raises(UpstreamStatusError):
        await client.get("x", expect=nothing)
    with pytest.raises(CircuitOpenError):
        await client.get("x", expect=nothing)

    assert breaker_states(registry)[_HOST].state is BreakerState.OPEN
    assert _HOST not in DEFAULT_REGISTRY
    assert len(fake.calls) == 1


async def test_a_client_with_its_own_registry_keeps_it() -> None:
    """Leave a client configured with a registry of its own alone."""
    own = BreakerRegistry()
    fake = FakeTransport().route("GET", "/x", 503)
    with isolated_breakers() as registry:
        client = _client(fake, BreakerPolicy(failure_threshold=1, registry=own))
    with pytest.raises(UpstreamStatusError):
        await client.get("x", expect=nothing)
    assert _HOST in own
    assert _HOST not in registry


async def test_a_client_without_a_breaker_stays_without_one() -> None:
    """Build no breaker for a client whose policy turns the breaker off."""
    fake = FakeTransport().route("GET", "/x", 503)
    with isolated_breakers() as registry:
        client = StubClient(
            base_url=f"https://{_HOST}",
            rate=Rate.per_second(1000),
            policy=TransportPolicy(retry=NO_RETRY, breaker=None),
            http=HttpOptions(transport=fake),
        )
    with pytest.raises(UpstreamStatusError):
        await client.get("x", expect=nothing)
    assert len(registry) == 0


async def test_the_registry_reads_the_test_clock() -> None:
    """Run a breaker's cooldown on the test clock it was built under."""
    fake = FakeTransport().route("GET", "/x", 503, 200)
    clock = TestClock()
    with clock.install(), isolated_breakers() as registry:
        client = _client(fake)
    with pytest.raises(UpstreamStatusError):
        await client.get("x", expect=nothing)
    assert registry[_HOST].refusing

    clock.advance(_COOLDOWN)
    assert not registry[_HOST].refusing
    await client.get("x", expect=nothing)
    assert registry[_HOST].state is BreakerState.CLOSED
