# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""Each client keeps its breakers to itself; clients share them only through a ``BreakerRegistry`` passed to each."""

from __future__ import annotations

import pytest

import headgate.breaker
import headgate.testing
from headgate import BreakerPolicy, Client, HttpOptions, Rate, TransportPolicy, nothing
from headgate.breaker import BreakerRegistry, BreakerState, CircuitBreaker
from headgate.errors import CircuitOpenError, UpstreamStatusError
from headgate.health import BreakerSnapshot, breaker_states
from headgate.testing import FakeTransport, TestClock, breakers_of
from tests.support.breakers import registry_of

_HOST = "flaky.test"
_ORIGIN = f"https://{_HOST}"
_COOLDOWN = 30.0
#: One failure opens the breaker.
_TRIP_AT_ONCE = BreakerPolicy(failure_threshold=1, recovery_timeout=_COOLDOWN)


def _client(fake: FakeTransport, breaker: BreakerPolicy | None = _TRIP_AT_ONCE) -> Client:
    """Build a client for :data:`_HOST` that sends once and trips its breaker on the first failure."""
    return Client(
        base_url=_ORIGIN,
        rate=Rate.per_second(1000),
        policy=TransportPolicy(retry=None, breaker=breaker),
        http=HttpOptions(transport=fake),
    )


async def _fail(client: Client) -> None:
    """Make one call the server answers 503."""
    with pytest.raises(UpstreamStatusError):
        await client.get("x", expect=nothing)


async def test_two_clients_given_no_registry_keep_a_breaker_each() -> None:
    """Leave the second client's breaker closed after the first client's failure opened its own."""
    first, second = _client(FakeTransport().reply("GET", "/x", 503)), _client(FakeTransport().reply("GET", "/x", 200))
    await _fail(first)

    await second.get("x", expect=nothing)

    assert registry_of(first) is not registry_of(second)
    assert breaker_states(registry_of(first)) == {_ORIGIN: BreakerSnapshot(BreakerState.OPEN, refusing=True)}


async def test_two_clients_given_one_registry_share_its_breakers() -> None:
    """Refuse the second client's call once the first client's failure opened the breaker they share."""
    shared = BreakerRegistry()
    policy = BreakerPolicy(failure_threshold=1, recovery_timeout=_COOLDOWN, registry=shared)
    second_fake = FakeTransport().reply("GET", "/x", 200)
    first, second = _client(FakeTransport().reply("GET", "/x", 503), policy), _client(second_fake, policy)
    await _fail(first)

    with pytest.raises(CircuitOpenError):
        await second.get("x", expect=nothing)

    assert breakers_of(first) is breakers_of(second) is shared
    assert not second_fake.calls
    assert breaker_states(shared)[_ORIGIN].state is BreakerState.OPEN


def test_a_registry_refuses_a_client_with_other_thresholds() -> None:
    """Raise when the second client is built, naming the thresholds the registry trips at and the ones asked."""
    shared = BreakerRegistry()
    _client(FakeTransport(), BreakerPolicy(failure_threshold=5, recovery_timeout=30.0, registry=shared))
    expected = (
        r"^this BreakerRegistry's breakers trip after 5 failures with a 30s cooldown, so a client with "
        r"BreakerPolicy\(failure_threshold=3, recovery_timeout=30\) cannot share it: give that client the same "
        r"thresholds, or a registry of its own$"
    )

    with pytest.raises(ValueError, match=expected):
        _client(FakeTransport(), BreakerPolicy(failure_threshold=3, recovery_timeout=30.0, registry=shared))


def test_a_client_without_a_breaker_has_no_registry() -> None:
    """Report no registry for a client whose policy turns the breaker off."""
    assert breakers_of(_client(FakeTransport(), None)) is None


async def test_the_registry_reads_the_test_clock() -> None:
    """Run a breaker's cooldown on the test clock the client was built under."""
    fake = FakeTransport().reply("GET", "/x", 503, 200)
    clock = TestClock()
    with clock.install():
        client = _client(fake)
    registry = registry_of(client)
    await _fail(client)
    assert _only(registry).refusing

    clock.advance(_COOLDOWN)
    assert not _only(registry).refusing
    await client.get("x", expect=nothing)
    assert _only(registry).state is BreakerState.CLOSED


def test_there_is_no_process_wide_registry() -> None:
    """Expose neither the old default registry nor the helper that swapped it out."""
    assert not hasattr(headgate.breaker, "DEFAULT_REGISTRY")
    assert not hasattr(headgate.testing, "isolated_breakers")


def _only(registry: BreakerRegistry) -> CircuitBreaker:
    """Return the one breaker *registry* holds for the test origin."""
    (breaker,) = registry.breakers(_ORIGIN)
    return breaker
