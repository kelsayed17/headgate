# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""``isolated_breakers``: the clients built in a block keep their breakers in a registry of their own."""

from __future__ import annotations

import pytest

from headgate import BreakerPolicy, HttpOptions, Rate, TransportPolicy, nothing
from headgate.breaker import BreakerRegistry, BreakerState, CircuitBreaker
from headgate.breaker._registry import DEFAULT_REGISTRY
from headgate.errors import CircuitOpenError, UpstreamStatusError
from headgate.health import breaker_states
from headgate.retry import NO_RETRY
from headgate.testing import FakeTransport, TestClock, isolated_breakers
from tests.support.clients import StubClient

_HOST = "flaky.test"
_ORIGIN = f"https://{_HOST}"
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
    fake = FakeTransport().reply("GET", "/x", 503)
    with isolated_breakers() as registry:
        client = _client(fake)
    with pytest.raises(UpstreamStatusError):
        await client.get("x", expect=nothing)
    with pytest.raises(CircuitOpenError):
        await client.get("x", expect=nothing)

    assert breaker_states(registry)[_ORIGIN].state is BreakerState.OPEN
    assert not DEFAULT_REGISTRY.breakers(_ORIGIN)
    assert len(fake.calls) == 1


async def test_a_client_with_its_own_registry_keeps_it() -> None:
    """Leave a client configured with a registry of its own alone."""
    own = BreakerRegistry()
    fake = FakeTransport().reply("GET", "/x", 503)
    with isolated_breakers() as registry:
        client = _client(fake, BreakerPolicy(failure_threshold=1, registry=own))
    with pytest.raises(UpstreamStatusError):
        await client.get("x", expect=nothing)
    assert own.breakers(_ORIGIN)
    assert not registry.breakers(_ORIGIN)


async def test_a_client_without_a_breaker_stays_without_one() -> None:
    """Build no breaker for a client whose policy turns the breaker off."""
    fake = FakeTransport().reply("GET", "/x", 503)
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
    fake = FakeTransport().reply("GET", "/x", 503, 200)
    clock = TestClock()
    with clock.install(), isolated_breakers() as registry:
        client = _client(fake)
    with pytest.raises(UpstreamStatusError):
        await client.get("x", expect=nothing)
    assert _only(registry).refusing

    clock.advance(_COOLDOWN)
    assert not _only(registry).refusing
    await client.get("x", expect=nothing)
    assert _only(registry).state is BreakerState.CLOSED


def _only(registry: BreakerRegistry) -> CircuitBreaker:
    """Return the one breaker *registry* holds for the test origin."""
    (breaker,) = registry.breakers(_ORIGIN)
    return breaker
