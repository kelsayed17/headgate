"""Tests for the async circuit breaker's state machine.

These drive the breaker directly. The seam it is driven THROUGH — one fetch per
``with breaker.track()`` block inside ``AsyncBaseClient._send``, spanning the body read —
is covered end-to-end in ``test_circuit_breaker.py``.

Written against ``breaker.call(coro_fn)`` Previously when the breaker stopped
wrapping a callable and became a context manager. Now the verdict rides on the
``Verdict`` the block marks, not on what escapes it: only a marked raise is a host failure,
and an unmarked one — or a ``CancelledError`` — records nothing. See
``CircuitBreaker.track``.
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest

from headgate._breaker import (
    HOST_BREAKERS,
    NO_BREAKERS,
    BreakerState,
    CircuitBreaker,
    CircuitOpenError,
)

#: The half-open tests' cooldown, and a sleep that outlasts it. The breaker reads ``time.monotonic``, which
#: time-machine does not patch, so the cooldown really elapses. No test here checks a refusal INSIDE the cooldown, only what the breaker
#: does once it has elapsed, and ``state`` reads OPEN until a probe is taken whatever the clock says.
_COOLDOWN = 0.001
_PAST_COOLDOWN = 0.005


class _Boom(Exception):
    """Test-only failure marker."""


def _fail(cb: CircuitBreaker, times: int = 1) -> None:
    """Record *times* host failures: a MARKED raise, which is what ``_base._send`` does for a wire failure."""
    for _ in range(times):
        with pytest.raises(_Boom), cb.track() as verdict:
            verdict.mark_failed()
            raise _Boom("nope")


def test_threshold_trips_breaker() -> None:
    cb = CircuitBreaker(failure_threshold=5, recovery_timeout=10.0, name="t")
    _fail(cb, 5)
    assert cb.state is BreakerState.OPEN


def test_success_resets_the_failure_count() -> None:
    cb = CircuitBreaker(failure_threshold=5, recovery_timeout=10.0, name="t")
    _fail(cb, 4)
    with cb.track():
        pass
    assert cb.failure_count == 0
    assert cb.state is BreakerState.CLOSED


def test_open_refuses_before_the_block_runs() -> None:
    cb = CircuitBreaker(failure_threshold=3, recovery_timeout=10.0, name="t")
    _fail(cb, 3)
    ran = False

    with pytest.raises(CircuitOpenError), cb.track():
        ran = True  # pragma: no cover - the point is that this never executes

    assert ran is False


async def test_half_open_probe_closes_on_success() -> None:
    cb = CircuitBreaker(failure_threshold=2, recovery_timeout=_COOLDOWN, name="t")
    _fail(cb, 2)
    assert cb.state is BreakerState.OPEN

    await asyncio.sleep(_PAST_COOLDOWN)
    with cb.track():
        pass

    assert cb.state is BreakerState.CLOSED


async def test_half_open_probe_failure_reopens() -> None:
    """One chance: a failed probe re-opens immediately, without re-reaching the threshold."""
    cb = CircuitBreaker(failure_threshold=2, recovery_timeout=_COOLDOWN, name="t")
    _fail(cb, 2)
    await asyncio.sleep(_PAST_COOLDOWN)

    _fail(cb, 1)

    assert cb.state is BreakerState.OPEN


async def test_half_open_gates_to_a_single_probe() -> None:
    """While the canary is in flight, concurrent callers are refused rather than racing in."""
    cb = CircuitBreaker(failure_threshold=2, recovery_timeout=_COOLDOWN, name="t")
    _fail(cb, 2)
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

    with pytest.raises(CircuitOpenError), cb.track():
        ran_concurrently = True  # pragma: no cover

    release.set()

    assert await probe_task == "probe"
    assert ran_concurrently is False
    assert cb.state is BreakerState.CLOSED


async def test_a_cancelled_probe_neither_records_nor_holds_the_slot() -> None:
    """The defect that made the original breaker unusable in the stack.

    ``except Exception`` does not catch a ``CancelledError``, so a cancelled canary left
    ``_probe_in_flight`` set and the breaker refused every caller forever.
    """
    cb = CircuitBreaker(failure_threshold=2, recovery_timeout=_COOLDOWN, name="t")
    _fail(cb, 2)
    await asyncio.sleep(_PAST_COOLDOWN)

    async def _probe_that_is_cancelled() -> None:
        with cb.track():
            await asyncio.sleep(3600)

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(_probe_that_is_cancelled(), timeout=0.02)

    assert cb.failure_count == 2, "a cancelled call records neither success nor failure"
    with cb.track():
        pass
    assert cb.state is BreakerState.CLOSED, "the next caller gets the probe slot"


def test_an_unmarked_exception_records_nothing() -> None:
    """An auth flow failing to mint, a body over the cap: nothing the wire said about the host."""
    cb = CircuitBreaker(failure_threshold=2, recovery_timeout=10.0, name="t")
    for _ in range(5):
        with pytest.raises(_Boom), cb.track():
            raise _Boom("not a host failure")

    assert cb.failure_count == 0
    assert cb.state is BreakerState.CLOSED


def test_a_neutral_success_neither_resets_the_count_nor_counts() -> None:
    """A cache hit reached no network, so it must not wipe the failures already recorded."""
    cb = CircuitBreaker(failure_threshold=5, recovery_timeout=10.0, name="t")
    _fail(cb, 3)
    with cb.track() as verdict:
        verdict.mark_untested()

    assert cb.failure_count == 3


async def test_a_neutral_probe_frees_the_slot_without_closing() -> None:
    """A probe served from cache tested nothing: the breaker stays unrecovered, and the next caller probes."""
    cb = CircuitBreaker(failure_threshold=2, recovery_timeout=_COOLDOWN, name="t")
    _fail(cb, 2)
    await asyncio.sleep(_PAST_COOLDOWN)

    with cb.track() as verdict:
        verdict.mark_untested()

    assert cb.state is not BreakerState.CLOSED
    with cb.track():  # the next caller is admitted as the probe, and its success closes the breaker
        pass
    assert cb.state is BreakerState.CLOSED


async def test_a_stale_call_cancelled_after_the_trip_does_not_free_the_probe_slot() -> None:
    """A call admitted while CLOSED must not admit a SECOND concurrent probe when it is cancelled later."""
    cb = CircuitBreaker(failure_threshold=1, recovery_timeout=_COOLDOWN, name="t")
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

    with pytest.raises(CircuitOpenError), cb.track():
        pass  # pragma: no cover - a second probe must not be admitted

    release.set()
    await probe
    assert cb.state is BreakerState.CLOSED


async def test_a_stale_failure_does_not_reopen_over_the_probe_in_flight() -> None:
    """A stale call's failure says nothing about the recovery test that is already out."""
    cb = CircuitBreaker(failure_threshold=1, recovery_timeout=_COOLDOWN, name="t")
    stale_started = asyncio.Event()
    fail_now = asyncio.Event()

    async def _stale() -> None:
        with cb.track() as verdict:
            stale_started.set()
            await fail_now.wait()
            verdict.mark_failed()
            raise _Boom("late")

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
    with pytest.raises(_Boom):
        await stale
    assert cb.state is BreakerState.HALF_OPEN, "the stale failure must leave the probe's test alone"

    release.set()
    await probe
    assert cb.state is BreakerState.CLOSED


def test_no_breakers_admits_every_call_and_never_reads_the_shared_registry() -> None:
    """A client with no breaker records nothing, and is never refused by one another client opened on its host."""
    HOST_BREAKERS.clear()
    breaker = NO_BREAKERS["api.example"]
    for _ in range(10):
        with contextlib.suppress(RuntimeError), breaker.track() as verdict:
            verdict.mark_failed()
            raise RuntimeError
    with breaker.track():
        pass  # still admitted after ten marked failures
    assert "api.example" not in HOST_BREAKERS
