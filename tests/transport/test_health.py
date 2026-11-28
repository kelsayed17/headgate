# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""``headgate.health``: the read surface an application exports transport degradation through.

headgate neither logs nor emits metrics, so these snapshots are the only way an open breaker reaches a dashboard (a
broken cache reaches one as a ``CacheChecked`` event). Each test drives the real mechanism, a breaker tripped by
failures, rather than writing the registry directly, so a rename on either side breaks here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

import headgate.health
from headgate import BreakerPolicy
from headgate._core import OBSERVER_FAILURES
from headgate.breaker import BreakerRegistry, BreakerState
from headgate.breaker._registry import table_of
from headgate.health import BreakerSnapshot, breaker_snapshots, observer_failures

if TYPE_CHECKING:
    from headgate.breaker._breaker import CircuitBreaker

_HOST = "health.example.test"
_ORIGIN = f"https://{_HOST}"

#: The test breakers trip after this many consecutive failures, and one cooldown no test outlasts.
_THRESHOLD = 5
_LONG_COOLDOWN = 3600.0


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


def test_breaker_snapshots_reports_a_tripped_host_as_open() -> None:
    """Report a host's breaker as closed and admitting, then open and refusing once it trips."""
    registry = BreakerRegistry(BreakerPolicy(failure_threshold=_THRESHOLD, recovery_timeout=_LONG_COOLDOWN))
    breaker = table_of(registry).breaker_for(_ORIGIN)
    assert breaker_snapshots(registry)[_ORIGIN] == BreakerSnapshot(
        state=BreakerState.CLOSED, refusing=False, failures=0
    )

    _trip(breaker)

    assert breaker_snapshots(registry)[_ORIGIN] == BreakerSnapshot(
        state=BreakerState.OPEN, refusing=True, failures=_THRESHOLD
    )


def test_a_tripped_breaker_past_its_cooldown_is_open_but_not_refusing() -> None:
    """Report open but not refusing: ``state`` waits for a probe, ``refusing`` follows the cooldown."""
    registry = BreakerRegistry(BreakerPolicy(failure_threshold=_THRESHOLD, recovery_timeout=0.0))
    _trip(table_of(registry).breaker_for(_ORIGIN))

    assert breaker_snapshots(registry)[_ORIGIN] == BreakerSnapshot(
        state=BreakerState.OPEN, refusing=False, failures=_THRESHOLD
    )


def test_a_snapshot_counts_failures() -> None:
    """Report the consecutive failures beside the state, in a snapshot that is a value, not a tuple."""
    registry = BreakerRegistry(BreakerPolicy(failure_threshold=3, recovery_timeout=_LONG_COOLDOWN))
    breaker = table_of(registry).breaker_for(_ORIGIN)
    for _ in range(2):
        with pytest.raises(ConnectionError):
            _fail_once(breaker)

    snapshot = breaker_snapshots(registry)[_ORIGIN]

    assert snapshot == BreakerSnapshot(state=BreakerState.CLOSED, refusing=False, failures=2)
    assert not isinstance(snapshot, tuple)


@pytest.mark.parametrize("name", ["state", "refusing", "failures"])
def test_a_snapshot_cannot_be_changed(name: str) -> None:
    """Refuse to rebind any field of a snapshot: it is what the breaker was at one instant."""
    snapshot = BreakerSnapshot(state=BreakerState.OPEN, refusing=True, failures=5)
    with pytest.raises(AttributeError):
        setattr(snapshot, name, None)


def test_breaker_snapshots_is_a_copy() -> None:
    """Return a fresh mapping each call, so clearing a snapshot leaves the registry intact."""
    registry = BreakerRegistry()
    table_of(registry).breaker_for(_ORIGIN)
    breaker_snapshots(registry).clear()
    assert _ORIGIN in breaker_snapshots(registry)


def test_the_breakers_reader_is_named_for_the_snapshots_it_returns() -> None:
    """Name it ``breaker_snapshots``: its values are snapshots, so code comparing one to a state fails at the import.

    ``breaker_states``, whose values code written for 0.12.1's named tuples may compare to a ``BreakerState``, is gone
    with no alias, since such a comparison is silently ``False``.
    """
    assert "breaker_states" not in vars(headgate.health)
    assert "breaker_snapshots" in headgate.health.__all__


def test_health_keeps_no_cache_count() -> None:
    """Leave a broken cache to the ``CacheChecked`` event, which names the call: no global count repeats it."""
    assert not hasattr(headgate.health, "cache_degradations")


def test_observer_failures_is_a_copy() -> None:
    """Return a fresh mapping each call, so clearing a snapshot leaves the count intact."""
    snapshot = observer_failures()
    snapshot.clear()
    assert observer_failures() == dict(OBSERVER_FAILURES)
