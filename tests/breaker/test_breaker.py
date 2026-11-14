# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""The circuit breaker's state machine, driven directly.

The seam it is driven through (one ``with breaker.track()`` block per fetch inside ``Client._exchange``,
spanning the body read) is covered end-to-end in ``test_breaker_integration.py``.

The outcome of a block is decided by the ``Verdict`` it marks, not by what escapes it: only a marked raise is a host
failure, cancelled or not, and an unmarked raise or ``CancelledError`` records nothing. See ``CircuitBreaker.track``.

The half-open tests' breaker reads the real ``time.monotonic``, so its cooldown really elapses. No test here checks a
refusal inside the cooldown, only what the breaker does once it has elapsed, and ``state`` reads open until a probe is
taken whatever the clock says.
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest

from headgate import BreakerPolicy
from headgate.breaker import BreakerRegistry, BreakerState, CircuitBreaker
from headgate.breaker._breaker import BreakerOpenError, Transition
from headgate.breaker._registry import NO_BREAKER
from headgate.timing import Timing

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
    """Run one tracked fetch that marks a wire failure and raises, as ``Client`` does for one."""
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


def test_a_marked_failure_counts_when_the_block_is_then_cancelled() -> None:
    """Count a marked failure however the block exits, a ``CancelledError`` included (a deadline cutting it off)."""
    cb = _breaker(1)

    def _cancelled_after_the_mark() -> None:
        with cb.track() as verdict:
            verdict.mark_failed()
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        _cancelled_after_the_mark()

    assert cb.state is BreakerState.OPEN


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


def test_no_breakers_admits_every_call() -> None:
    """Record nothing without a breaker: ten marked failures later it still admits."""
    breaker = NO_BREAKER
    for _ in range(10):
        with contextlib.suppress(RuntimeError), breaker.track() as verdict:
            verdict.mark_failed()
            raise RuntimeError
    with breaker.track():
        pass  # still admitted after ten marked failures


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
    cb = CircuitBreaker(failure_threshold=1, recovery_timeout=_LONG_COOLDOWN, name="t", timing=Timing(clock=clock))
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


def test_a_registry_keys_a_breaker_by_origin() -> None:
    """Share one breaker per origin, however the URL spells it; refuse other thresholds; never build on a read."""
    registry = BreakerRegistry()
    first = registry.breaker_for("https://a.example/x", failure_threshold=1, recovery_timeout=_LONG_COOLDOWN)
    again = registry.breaker_for("HTTPS://A.example:443/y?q", failure_threshold=1, recovery_timeout=_LONG_COOLDOWN)
    other = registry.breaker_for("https://b.example", failure_threshold=1, recovery_timeout=_LONG_COOLDOWN)
    assert first is again
    assert (first.name, first.failure_threshold, first.recovery_timeout) == ("https://a.example", 1, _LONG_COOLDOWN)
    with pytest.raises(ValueError, match="trip after 1 failures"):
        registry.breaker_for("https://a.example", failure_threshold=_THRESHOLD, recovery_timeout=_COOLDOWN)
    _fail(first)
    assert (first.state, other.state) == (BreakerState.OPEN, BreakerState.CLOSED)

    assert registry.breakers("http://a.example") == ()
    assert registry.breakers("https://a.example") == (first,)
    assert list(registry) == [first, other]
    assert len(registry) == len((first, other))
    assert repr(registry) == "BreakerRegistry({'https://a.example': 'open', 'https://b.example': 'closed'})"

    registry.clear()
    assert len(registry) == 0


def test_a_registry_breakers_read_its_clock() -> None:
    """Hand the registry's clock to every breaker it builds, so a test can drive their cooldowns."""
    clock = _Clock()
    registry = BreakerRegistry(timing=Timing(clock=clock))
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


def test_a_breaker_policy_names_no_registry_by_default() -> None:
    """Leave the registry unnamed, so each client builds one of its own."""
    assert BreakerPolicy().registry is None


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
    cb = CircuitBreaker(failure_threshold=1, recovery_timeout=_LONG_COOLDOWN, timing=Timing(clock=clock))
    _fail(cb)
    clock.now = _LONG_COOLDOWN
    with cb.track() as verdict:
        verdict.mark_failed()
    assert cb.state is BreakerState.OPEN


def _clocked(threshold: int = 1) -> tuple[CircuitBreaker, _Clock]:
    """Build a breaker for ``t`` whose cooldown a test elapses by hand."""
    clock = _Clock()
    return CircuitBreaker(
        failure_threshold=threshold, recovery_timeout=_LONG_COOLDOWN, name="t", timing=Timing(clock=clock)
    ), clock


def test_a_call_admitted_before_the_trip_is_ignored_after_the_probe_closes() -> None:
    """Record nothing for a call from before the trip, even once the probe has closed the breaker again."""
    cb, clock = _clocked()
    with contextlib.ExitStack() as stale:
        verdict = stale.enter_context(cb.track())
        _fail(cb)
        clock.now = _LONG_COOLDOWN
        with cb.track():
            pass
        assert cb.state is BreakerState.CLOSED
        verdict.mark_failed()
    assert cb.state is BreakerState.CLOSED, "a stale failure must not trip the recovered breaker again"
    assert cb.failure_count == 0


def test_a_call_admitted_before_the_trip_is_refused_its_retries() -> None:
    """Refuse a re-send from before the trip once the cooldown elapses: it would reach the origin beside the probe."""
    cb, clock = _clocked()
    with cb.track() as stale:
        _fail(cb)
        clock.now = _LONG_COOLDOWN
        assert not cb.refusing, "a new call would be admitted as the probe"
        assert stale.refused()
        with cb.track() as probe:
            assert not probe.refused(), "the probe keeps its retries"
        with cb.track() as fresh:
            assert not fresh.refused()
        assert stale.refused(), "the breaker closing again does not make the stale call current"


def test_a_listener_raising_on_the_probe_transition_frees_the_probe_slot() -> None:
    """Free the probe slot when the listener told of taking it raises, so the breaker does not refuse forever."""
    cb, clock = _clocked()
    _fail(cb)
    clock.now = _LONG_COOLDOWN

    def _raise(_: object) -> None:
        raise _BoomError

    with pytest.raises(_BoomError), cb.track(_raise):
        pytest.fail("the block must not run when the listener raised")
    assert not cb.refusing
    with cb.track():
        pass
    assert cb.state is BreakerState.CLOSED


def test_an_answered_probe_closes_the_breaker_before_its_block_exits() -> None:
    """Settle a probe once its origin answered, and count a later failure as a closed breaker's."""
    cb, clock = _clocked(threshold=2)
    _fail(cb, 2)
    clock.now = _LONG_COOLDOWN
    changes: list[Transition] = []
    with cb.track(changes.append) as probe:
        probe.mark_answered()
        assert cb.state is BreakerState.CLOSED
        assert [change.state for change in changes] == [BreakerState.HALF_OPEN, BreakerState.CLOSED]
        with cb.track():
            pass  # admitted while the probe's body is still being read
        probe.mark_failed()
    assert cb.state is BreakerState.CLOSED, "a mid-body failure is one ordinary failure, not a failed probe"
    assert cb.failure_count == 1


def test_an_answered_call_records_its_head_once() -> None:
    """Record a closed call's answer at its head, and nothing more for a raise the block then makes unmarked."""
    cb, _ = _clocked(threshold=2)
    _fail(cb)
    with contextlib.suppress(_BoomError), cb.track() as verdict:
        verdict.mark_answered()
        assert cb.failure_count == 0
        raise _BoomError
    assert cb.failure_count == 0


def test_an_untested_answered_probe_frees_the_slot_without_closing() -> None:
    """Free the probe slot at the head of a response the cache served, leaving the breaker for the next call to test."""
    cb, clock = _clocked()
    _fail(cb)
    clock.now = _LONG_COOLDOWN
    with cb.track() as probe:
        probe.mark_untested()
        probe.mark_answered()
        assert not cb.refusing
        probe.mark_failed()
    assert cb.state is BreakerState.HALF_OPEN
    with cb.track():
        pass
    assert cb.state is BreakerState.CLOSED


def test_an_answered_probe_that_failed_reopens_and_records_nothing_after() -> None:
    """Re-open at the head of a failing answer (a 501), and ignore the rest of that block."""
    cb, clock = _clocked()
    _fail(cb)
    clock.now = _LONG_COOLDOWN
    with cb.track() as probe:
        probe.mark_failed()
        probe.mark_answered()
        assert cb.state is BreakerState.OPEN
    assert cb.state is BreakerState.OPEN
    assert cb.refusing


def test_no_breaker_takes_an_answer_and_refuses_nothing() -> None:
    """Accept an answer and never refuse a re-send without a breaker."""
    with NO_BREAKER.track() as verdict:
        verdict.mark_answered()
        assert not verdict.refused()
