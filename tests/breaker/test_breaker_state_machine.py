# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""The circuit breaker keeps its invariants under any interleaving of concurrent calls, outcomes and elapsed time.

Hypothesis drives a :class:`~hypothesis.stateful.RuleBasedStateMachine`: calls enter ``track()`` and stay open, so
several are in flight at once; each later finishes with one of the outcomes a fetch can have; and the injected clock
jumps forward. After every step the machine checks what the breaker promises.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, override

from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, initialize, invariant, precondition, rule

from headgate.breaker import BreakerOpenError, BreakerState, CircuitBreaker, Verdict

if TYPE_CHECKING:
    from contextlib import AbstractContextManager

_THRESHOLD = 3
_COOLDOWN = 10.0
_OUTCOMES = ("success", "marked_failure", "marked_failure_caught", "unmarked_error", "cancelled", "untested")


class _FetchError(Exception):
    """What a failing fetch raises inside ``track()``."""


@dataclass
class _Call:
    """One fetch inside ``track()``: its open scope, its verdict, and whether it took the probe slot."""

    scope: AbstractContextManager[Verdict]
    verdict: Verdict
    probe: bool


class _Clock:
    """A clock the test advances by hand."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        """Return the current time."""
        return self.now


class BreakerMachine(RuleBasedStateMachine):
    """Drive one breaker through random sequences of calls, outcomes and time."""

    def __init__(self) -> None:
        """Start with no breaker; :meth:`build` creates it."""
        super().__init__()
        self.clock = _Clock()
        self.breaker = CircuitBreaker(failure_threshold=_THRESHOLD, recovery_timeout=_COOLDOWN, clock=self.clock)
        self.open_calls: list[_Call] = []
        self.opened_at: float | None = None

    @initialize()
    def build(self) -> None:
        """Begin closed, with nothing in flight."""
        assert self.breaker.state is BreakerState.CLOSED

    @rule()
    def begin_call(self) -> None:
        """Enter ``track()`` for a new fetch, which is admitted or refused."""
        was_closed = self.breaker.state is BreakerState.CLOSED
        scope = self.breaker.track()
        try:
            verdict = scope.__enter__()
        except BreakerOpenError:
            assert not was_closed, "a closed breaker must admit every call"
            return
        self.open_calls.append(_Call(scope, verdict, probe=not was_closed))

    @precondition(lambda self: self.open_calls)
    @rule(data=st.data(), outcome=st.sampled_from(_OUTCOMES))
    def finish_call(self, data: st.DataObject, outcome: str) -> None:
        """Finish one in-flight fetch, chosen at random, with *outcome*."""
        index = data.draw(st.integers(min_value=0, max_value=len(self.open_calls) - 1))
        call = self.open_calls.pop(index)
        was_open = self.breaker.state is BreakerState.OPEN
        if outcome == "success":
            call.scope.__exit__(None, None, None)
        elif outcome == "untested":
            call.verdict.mark_untested()
            call.scope.__exit__(None, None, None)
        elif outcome == "marked_failure":
            call.verdict.mark_failed()
            call.scope.__exit__(_FetchError, _FetchError("wire failed"), None)
        elif outcome == "marked_failure_caught":
            # The caller caught the failure inside the block and left it normally.
            counted_before = self.breaker.failure_count
            closed_before = self.breaker.state is BreakerState.CLOSED
            call.verdict.mark_failed()
            call.scope.__exit__(None, None, None)
            if call.probe:
                assert self.breaker.state is BreakerState.OPEN, "a probe whose failure was caught still re-opens"
            elif closed_before:
                counted = self.breaker.failure_count == counted_before + 1
                assert counted or self.breaker.state is BreakerState.OPEN, "a caught failure still counts"
        elif outcome == "unmarked_error":
            call.scope.__exit__(_FetchError, _FetchError("not a host failure"), None)
        else:
            call.scope.__exit__(asyncio.CancelledError, asyncio.CancelledError(), None)
        if self.breaker.state is BreakerState.OPEN and not was_open:
            self.opened_at = self.clock.now

    @rule(seconds=st.floats(min_value=0, max_value=3 * _COOLDOWN))
    def advance_clock(self, seconds: float) -> None:
        """Let time pass."""
        self.clock.now += seconds

    @invariant()
    def at_most_one_probe_is_in_flight(self) -> None:
        """Admit no second probe while one is out."""
        assert sum(call.probe for call in self.open_calls) <= 1

    @invariant()
    def a_closed_breaker_is_below_its_threshold(self) -> None:
        """Trip as soon as the consecutive failures reach the threshold."""
        if self.breaker.state is BreakerState.CLOSED:
            assert self.breaker.failure_count < _THRESHOLD

    @invariant()
    def an_open_breaker_refuses_within_its_cooldown(self) -> None:
        """Refuse every call while an open breaker's cooldown has not elapsed."""
        opened_at = self.opened_at
        if self.breaker.state is BreakerState.OPEN and opened_at is not None and self.clock.now - opened_at < _COOLDOWN:
            assert self.breaker.refusing

    @invariant()
    def a_closed_breaker_never_refuses(self) -> None:
        """Admit every call while closed."""
        if self.breaker.state is BreakerState.CLOSED:
            assert not self.breaker.refusing

    @override
    def teardown(self) -> None:
        """Close every scope still open, as the fetches would on cancellation."""
        for call in self.open_calls:
            call.scope.__exit__(asyncio.CancelledError, asyncio.CancelledError(), None)


TestBreakerMachine = BreakerMachine.TestCase
