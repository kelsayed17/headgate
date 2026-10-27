# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""The circuit breaker's state machine, driven directly.

The seam it is driven through (one ``with breaker.track()`` block per fetch inside ``AsyncBaseClient._exchange``,
spanning the body read) is covered end-to-end in ``test_breaker_integration.py``.

The outcome of a block is decided by the ``Verdict`` it marks, not by what escapes it: only a marked raise is a host
failure, and an unmarked raise or a ``CancelledError`` records nothing. See ``CircuitBreaker.track``.

The half-open tests' breaker reads the real ``time.monotonic``, so its cooldown really elapses. No test here checks a
refusal inside the cooldown, only what the breaker does once it has elapsed, and ``state`` reads open until a probe is
taken whatever the clock says.
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest

from headgate.breaker import BreakerPolicy, BreakerRegistry, BreakerState, CircuitBreaker
from headgate.breaker._breaker import BreakerOpenError
from headgate.breaker._registry import DEFAULT_REGISTRY, NO_BREAKER

#: The half-open tests' cooldown, and a sleep that outlasts it.
_COOLDOWN = 0.001
_PAST_COOLDOWN = 0.005

#: A cooldown no test outlasts.
_LONG_COOLDOWN = 10.0

#: The threshold the tests that trip a breaker use, and one less: the most failures a closed breaker can hold.
_THRESHOLD = 5
_BELOW_THRESHOLD = _THRESHOLD - 1

#: The small threshold the half-open tests trip quickly.
_QUICK = 2


class _BoomError(Exception):
    """Test-only failure marker."""


def _raise_marked(cb: CircuitBreaker) -> None:
    """Run one tracked fetch that marks a wire failure and raises, as ``AsyncBaseClient`` does for one."""
    with cb.track() as verdict:
        verdict.mark_failed()
        msg = "nope"
        raise _BoomError(msg)


def _raise_unmarked(cb: CircuitBreaker) -> None:
    """Run one tracked fetch that raises without marking anything, like an auth flow failing to mint."""
    with cb.track():
        msg = "not a host failure"
        raise _BoomError(msg)


def _fail(cb: CircuitBreaker, times: int = 1) -> None:
    """Record *times* host failures."""
    for _ in range(times):
        with pytest.raises(_BoomError):
            _raise_marked(cb)


def _breaker(threshold: int = _THRESHOLD, cooldown: float = _LONG_COOLDOWN) -> CircuitBreaker:
    """Build a breaker for the test host ``t``."""
    return CircuitBreaker(failure_threshold=threshold, recovery_timeout=cooldown, name="t")


def test_threshold_trips_breaker() -> None:
    """Open after the threshold's worth of consecutive failures."""
    cb = _breaker()
    _fail(cb, _THRESHOLD)
    assert cb.state is BreakerState.OPEN


def test_success_resets_the_failure_count() -> None:
    """Clear the consecutive-failure count on a success."""
    cb = _breaker()
    _fail(cb, _BELOW_THRESHOLD)
    with cb.track():
        pass
    assert cb.failure_count == 0
    assert cb.state is BreakerState.CLOSED


def test_open_refuses_before_the_block_runs() -> None:
    """Refuse an open breaker's call before its block runs, naming the host."""
    cb = _breaker()
    _fail(cb, _THRESHOLD)
    ran = False

    with pytest.raises(BreakerOpenError, match="open for t"), cb.track():
        ran = True

    assert ran is False


async def test_half_open_probe_closes_on_success() -> None:
    """Close the breaker when the probe admitted after the cooldown succeeds."""
    cb = _breaker(_QUICK, _COOLDOWN)
    _fail(cb, _QUICK)
    assert cb.state is BreakerState.OPEN

    await asyncio.sleep(_PAST_COOLDOWN)
    with cb.track():
        pass

    assert cb.state is BreakerState.CLOSED


async def test_half_open_probe_failure_reopens() -> None:
    """Re-open at once on a failed probe, without re-reaching the threshold: the host gets one chance."""
    cb = _breaker(_QUICK, _COOLDOWN)
    _fail(cb, _QUICK)
    await asyncio.sleep(_PAST_COOLDOWN)

    _fail(cb, 1)

    assert cb.state is BreakerState.OPEN


async def test_half_open_gates_to_a_single_probe() -> None:
    """Refuse concurrent callers while the probe is in flight, rather than letting them race in."""
    cb = _breaker(_QUICK, _COOLDOWN)
    _fail(cb, _QUICK)
    await asyncio.sleep(_PAST_COOLDOWN)

    probe_started = asyncio.Event()
    release = asyncio.Event()
    ran_concurrently = False

    async def _slow_probe() -> str:
        with cb.track():
            probe_started.set()
            await release.wait()
            return "probe"

    probe_task = asyncio.create_task(_slow_probe())
    await probe_started.wait()

    with pytest.raises(BreakerOpenError), cb.track():
        ran_concurrently = True

    release.set()

    assert await probe_task == "probe"
    assert ran_concurrently is False
    assert cb.state is BreakerState.CLOSED


async def test_a_cancelled_probe_neither_records_nor_holds_the_slot() -> None:
    """Release the probe slot when the probe is cancelled, or the breaker refuses every caller forever.

    ``CancelledError`` is a ``BaseException``, so ``except Exception`` alone would never settle the probe.
    """
    cb = _breaker(_QUICK, _COOLDOWN)
    _fail(cb, _QUICK)
    await asyncio.sleep(_PAST_COOLDOWN)

    async def _probe_that_is_cancelled() -> None:
        with cb.track():
            await asyncio.sleep(3600)

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(_probe_that_is_cancelled(), timeout=0.02)

    assert cb.failure_count == _QUICK, "a cancelled call records neither success nor failure"
    with cb.track():
        pass
    assert cb.state is BreakerState.CLOSED, "the next caller gets the probe slot"


def test_an_unmarked_exception_records_nothing() -> None:
    """Record nothing for an unmarked raise (an auth flow failing to mint, a body over the cap)."""
    cb = _breaker(_QUICK)
    for _ in range(_THRESHOLD):
        with pytest.raises(_BoomError):
            _raise_unmarked(cb)

    assert cb.failure_count == 0
    assert cb.state is BreakerState.CLOSED


def test_a_neutral_success_neither_resets_the_count_nor_counts() -> None:
    """Leave the count alone on an untested success: a cache hit reached no network."""
    cb = _breaker()
    _fail(cb, _BELOW_THRESHOLD)
    with cb.track() as verdict:
        verdict.mark_untested()

    assert cb.failure_count == _BELOW_THRESHOLD


async def test_a_neutral_probe_frees_the_slot_without_closing() -> None:
    """Free the slot without closing on a probe served from cache: it tested nothing, so the next caller probes."""
    cb = _breaker(_QUICK, _COOLDOWN)
    _fail(cb, _QUICK)
    await asyncio.sleep(_PAST_COOLDOWN)

    with cb.track() as verdict:
        verdict.mark_untested()

    assert cb.state is not BreakerState.CLOSED
    with cb.track():  # the next caller is admitted as the probe, and its success closes the breaker
        pass
    assert cb.state is BreakerState.CLOSED


async def test_a_stale_call_cancelled_after_the_trip_does_not_free_the_probe_slot() -> None:
    """Keep the probe slot taken when a call admitted while closed is cancelled after the trip."""
    cb = _breaker(1, _COOLDOWN)
    stale_started = asyncio.Event()

    async def _stale() -> None:
        with cb.track():
            stale_started.set()
            await asyncio.sleep(3600)

    stale = asyncio.create_task(_stale())
    await stale_started.wait()
    _fail(cb, 1)
    assert cb.state is BreakerState.OPEN
    await asyncio.sleep(_PAST_COOLDOWN)

    probe_started = asyncio.Event()
    release = asyncio.Event()

    async def _probe() -> None:
        with cb.track():
            probe_started.set()
            await release.wait()

    probe = asyncio.create_task(_probe())
    await probe_started.wait()

    stale.cancel()
    with pytest.raises(asyncio.CancelledError):
        await stale

    with pytest.raises(BreakerOpenError), cb.track():
        pass

    release.set()
    await probe
    assert cb.state is BreakerState.CLOSED


async def test_a_stale_failure_does_not_reopen_over_the_probe_in_flight() -> None:
    """Ignore a stale call's failure while a probe is out: it says nothing about the recovery test."""
    cb = _breaker(1, _COOLDOWN)
    stale_started = asyncio.Event()
    fail_now = asyncio.Event()

    async def _stale() -> None:
        with cb.track() as verdict:
            stale_started.set()
            await fail_now.wait()
            verdict.mark_failed()
            msg = "late"
            raise _BoomError(msg)

    stale = asyncio.create_task(_stale())
    await stale_started.wait()
    _fail(cb, 1)
    await asyncio.sleep(_PAST_COOLDOWN)

    probe_started = asyncio.Event()
    release = asyncio.Event()

    async def _probe() -> None:
        with cb.track():
            probe_started.set()
            await release.wait()

    probe = asyncio.create_task(_probe())
    await probe_started.wait()

    fail_now.set()
    with pytest.raises(_BoomError):
        await stale
    assert cb.state is BreakerState.HALF_OPEN, "the stale failure must leave the probe's test alone"

    release.set()
    await probe
    assert cb.state is BreakerState.CLOSED


def test_no_breakers_admits_every_call_and_never_reads_the_shared_registry() -> None:
    """Record nothing without a breaker, and never read the registry another client's breaker lives in."""
    DEFAULT_REGISTRY.clear()
    breaker = NO_BREAKER
    for _ in range(10):
        with contextlib.suppress(RuntimeError), breaker.track() as verdict:
            verdict.mark_failed()
            raise RuntimeError
    with breaker.track():
        pass  # still admitted after ten marked failures
    assert len(DEFAULT_REGISTRY) == 0


class _Clock:
    """A monotonic clock that moves only when a test advances it."""

    def __init__(self) -> None:
        """Start at zero."""
        self.now = 0.0

    def __call__(self) -> float:
        """Return the current reading."""
        return self.now


def test_a_refusal_says_how_long_the_cooldown_has_left() -> None:
    """Carry the seconds until a probe is admitted, and ``None`` while the probe is out."""
    clock = _Clock()
    cb = CircuitBreaker(failure_threshold=1, recovery_timeout=_LONG_COOLDOWN, name="t", clock=clock)
    _fail(cb)
    elapsed = 4.0
    clock.now = elapsed
    with pytest.raises(BreakerOpenError) as refused, cb.track():
        pass
    assert refused.value.retry_after == _LONG_COOLDOWN - elapsed

    nearly = _LONG_COOLDOWN - 0.25
    clock.now = nearly
    with pytest.raises(BreakerOpenError) as almost, cb.track():
        pass
    assert almost.value.retry_after == _LONG_COOLDOWN - nearly, "under a second left is said exactly"

    clock.now = _LONG_COOLDOWN
    with cb.track(), pytest.raises(BreakerOpenError) as during_probe, cb.track():
        pass
    assert during_probe.value.retry_after is None


def test_a_registry_keys_a_breaker_by_origin_and_thresholds() -> None:
    """Share one breaker per origin and thresholds; give other thresholds their own; never build on a read."""
    registry = BreakerRegistry()
    first = registry.breaker_for("https://a.example/x", failure_threshold=1, recovery_timeout=_LONG_COOLDOWN)
    again = registry.breaker_for("HTTPS://A.example:443/y?q", failure_threshold=1, recovery_timeout=_LONG_COOLDOWN)
    other = registry.breaker_for("https://a.example", failure_threshold=_THRESHOLD, recovery_timeout=_COOLDOWN)
    assert first is again, "the same origin, however the URL spells it, with the same thresholds"
    assert other is not first, "other thresholds get a breaker of their own, built with them"
    assert (other.failure_threshold, other.recovery_timeout) == (_THRESHOLD, _COOLDOWN)
    assert first.name == "https://a.example"
    _fail(first)
    assert (first.state, other.state) == (BreakerState.OPEN, BreakerState.CLOSED)

    assert registry.breakers("http://a.example") == ()
    assert registry.breakers("https://a.example") == (first, other)
    assert list(registry) == [first, other]
    assert len(registry) == len((first, other))

    registry.clear()
    assert len(registry) == 0


def test_a_registry_breakers_read_its_clock() -> None:
    """Hand the registry's clock to every breaker it builds, so a test can drive their cooldowns."""
    clock = _Clock()
    registry = BreakerRegistry(clock=clock)
    cb = registry.breaker_for("https://a.example", failure_threshold=1, recovery_timeout=_LONG_COOLDOWN)
    _fail(cb)
    assert cb.refusing
    clock.now = _LONG_COOLDOWN
    assert not cb.refusing


@pytest.mark.parametrize(
    ("threshold", "cooldown", "match"),
    [
        (0, _COOLDOWN, "failure_threshold"),
        (1, -1.0, "recovery_timeout"),
        (1, float("nan"), "recovery_timeout"),
        (1, float("inf"), "recovery_timeout"),
    ],
)
def test_a_breaker_policy_refuses_a_nonsense_value(threshold: int, cooldown: float, match: str) -> None:
    """Refuse a threshold below one and a cooldown that is negative, NaN or infinite."""
    with pytest.raises(ValueError, match=match):
        BreakerPolicy(failure_threshold=threshold, recovery_timeout=cooldown)


def test_a_breaker_policy_shares_the_default_registry_by_default() -> None:
    """Use the process-wide registry unless the policy names another."""
    assert BreakerPolicy().registry is DEFAULT_REGISTRY


def test_a_marked_failure_counts_even_when_the_block_exits_cleanly() -> None:
    """Record a failure the caller caught inside the tracked block: the wire failed, however the block was left.

    A caller reading a stream itself can catch the ``TransportError`` a dropped body raises and leave the block
    normally; settling that as a success would record a failing host as healthy.
    """
    cb = _breaker(threshold=1)
    with cb.track() as verdict:
        verdict.mark_failed()
    assert cb.state is BreakerState.OPEN


def test_a_probe_whose_failure_was_caught_reopens_the_breaker() -> None:
    """Re-open on a probe that marked a failure and exited cleanly, rather than closing on it."""
    clock = _Clock()
    cb = CircuitBreaker(failure_threshold=1, recovery_timeout=_LONG_COOLDOWN, clock=clock)
    _fail(cb)
    clock.now = _LONG_COOLDOWN
    with cb.track() as verdict:
        verdict.mark_failed()
    assert cb.state is BreakerState.OPEN
