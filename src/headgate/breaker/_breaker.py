# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""The circuit breaker: stop calling a host that is failing, find out cheaply when it recovers.

There is one breaker per host, shared by every client that uses the same :class:`~headgate.breaker.BreakerRegistry`
(by default the process-wide one), so two clients addressing the same upstream cannot each spend their own quota of
failures against it.

Three design choices, each fixing a failure mode of the usual ``with breaker:`` wrapper:

* **Only a marked failure counts.** An ``asyncio.CancelledError`` (what ``asyncio.wait_for`` raises around a call when
  the caller's timeout is shorter than the client's) records nothing, rather than clearing the failure count; so does
  an auth flow failing to mint or a body over the size cap. See :meth:`CircuitBreaker.track`.
* **Half-open admits one probe.** A host that has just failed gets one call as its recovery test, not the full
  concurrent load.
* **The breaker spans the body read.** ``AsyncBaseClient`` drives it around the send and the body read, so a connection
  dying mid-body is a failure of the fetch. A transport layer would see only the response headers, which is why there
  is no breaker transport.

The trip rule is ``failure_threshold`` consecutive failures, so any success resets the count, and a host failing half
its calls never trips. A failure-rate window over the last N calls is the obvious alternative if that is ever wanted.

There is no lock: every state change happens between awaits on one event loop, so there is no critical section. An
``asyncio.Lock`` would be wrong here besides, since it binds to the loop that first awaits it while a registry may be
process-global and outlive any one loop.
"""

from __future__ import annotations

import enum
import time
from contextlib import contextmanager
from typing import TYPE_CHECKING

from headgate._core import BreakerState

if TYPE_CHECKING:
    from collections.abc import Generator

    from headgate._core import Clock

__all__ = ["BreakerOpenError", "CircuitBreaker", "Verdict"]


class BreakerOpenError(Exception):
    """Raised by :meth:`CircuitBreaker.track` when it refuses a call: open, or its one probe already out.

    ``AsyncBaseClient`` raises it to its caller as :class:`headgate.errors.CircuitOpenError`. ``retry_after`` is how
    many seconds remain until the breaker admits a probe, or ``None`` while a probe is out (its outcome decides).
    """

    def __init__(self, message: str, *, retry_after: float | None) -> None:
        """Record *message* and *retry_after*."""
        super().__init__(message)
        self.retry_after = retry_after


class _Outcome(enum.Enum):
    """What one tracked call established about its host."""

    FAILURE = enum.auto()
    SUCCESS = enum.auto()
    NOTHING = enum.auto()


class Verdict:
    """What one fetch established about its host, marked by ``AsyncBaseClient`` inside :meth:`CircuitBreaker.track`.

    Unmarked means "nothing": only an explicit mark makes a raise a host failure, and only a clean exit that was not
    marked untested is a host success. See :meth:`CircuitBreaker.track` for why the default is not the reverse.
    """

    __slots__ = ("failed", "untested")

    def __init__(self) -> None:
        """Start unmarked."""
        self.failed = False
        self.untested = False

    def mark_failed(self) -> None:
        """Record that the wire failed: a transport error or timeout, or a 429/5xx that survived retries."""
        self.failed = True

    def mark_untested(self) -> None:
        """Record that the fetch reached no network (a cache hit), so its success says nothing about the host."""
        self.untested = True


class CircuitBreaker:
    """A closed / open / half-open breaker, driven by one ``with breaker.track()`` block per fetch."""

    __slots__ = (
        "_clock",
        "_failure_threshold",
        "_failures",
        "_name",
        "_opened_at",
        "_probe_in_flight",
        "_recovery_timeout",
        "_state",
    )

    def __init__(
        self,
        *,
        failure_threshold: int = 5,
        recovery_timeout: float = 60.0,
        name: str = "",
        clock: Clock = time.monotonic,
    ) -> None:
        """Trip after *failure_threshold* consecutive failures; admit a probe *recovery_timeout* seconds later.

        *name* (the host) appears in the :class:`BreakerOpenError` message. *clock* returns monotonic seconds; it is a
        parameter so the cooldown can be driven by a test without waiting.
        """
        self._clock = clock
        self._failure_threshold = failure_threshold
        self._recovery_timeout = recovery_timeout
        self._name = name
        self._state = BreakerState.CLOSED
        self._failures = 0
        self._opened_at = 0.0
        self._probe_in_flight = False

    @property
    def state(self) -> BreakerState:
        """The current state; an open breaker whose cooldown has elapsed reads open until a probe is taken."""
        return self._state

    @property
    def refusing(self) -> bool:
        """Whether a call made now would be refused: open inside its cooldown, or a probe already out.

        Unlike :attr:`state`, this goes false once the cooldown elapses, so a host nothing has called since it
        tripped does not read as refusing forever.
        """
        return not self._admits()

    @property
    def failure_count(self) -> int:
        """Consecutive failures recorded since the last success."""
        return self._failures

    def _cooldown_left(self) -> float | None:
        """Return the seconds until a probe is admitted, or ``None`` while one is out."""
        if self._probe_in_flight:
            return None
        return max(0.0, self._recovery_timeout - (self._clock() - self._opened_at))

    def _admits(self) -> bool:
        """Whether a call would be let through, without taking the probe slot."""
        if self._state is BreakerState.CLOSED:
            return True
        if self._probe_in_flight:
            return False
        return self._clock() - self._opened_at >= self._recovery_timeout

    @contextmanager
    def track(self) -> Generator[Verdict]:
        """Run one fetch through the breaker.

        Raises :class:`BreakerOpenError` instead of yielding when the host is being refused. Otherwise yields a
        :class:`Verdict` the fetch marks as it goes, and what the call records on the way out is:

        * **a host failure**: only when the fetch marked :meth:`Verdict.mark_failed` before raising, which
          ``AsyncBaseClient`` does for a transport error or timeout and for a 429/5xx that survived retries. Those are
          what the wire said about this host;
        * **a success**: a clean exit, unless the fetch marked :meth:`Verdict.mark_untested` (a cache hit, which
          reached no network). A success resets the consecutive-failure count;
        * **nothing**: everything else, meaning an exception the fetch did not mark (an auth flow failing to mint a
          token, a body over the size cap, a programming error) and a ``BaseException`` such as ``CancelledError``
          (the caller gave up). None of those says whether the host is up.

        The default is "nothing" rather than "failure" so that five token mints refused for a configuration reason
        cannot open the breaker for every client on that host, hiding the real error behind ``circuit breaker open``;
        and a clean exit is not a success when it never reached the host, so a cache hit cannot close a probe.

        Only the call that took the probe slot settles a probe. A call admitted while the breaker was closed that
        finishes after it tripped (realistic, since three 30 s attempts plus backoff outlast the cooldown) changes no
        state at all: its cancellation must not free the probe slot, admitting a second concurrent probe, and its
        failure must not re-open the breaker while the real probe is still out.
        """
        if not self._admits():
            msg = f"circuit breaker open for {self._name}"
            raise BreakerOpenError(msg, retry_after=self._cooldown_left())
        probing = self._state is not BreakerState.CLOSED
        if probing:
            # The cooldown has elapsed and this caller is the canary: take the slot so concurrent callers are refused
            # until it reports.
            self._state = BreakerState.HALF_OPEN
            self._probe_in_flight = True

        verdict = Verdict()
        settle = self._settle_probe if probing else self._settle_call
        try:
            yield verdict
        except Exception:
            settle(_Outcome.FAILURE if verdict.failed else _Outcome.NOTHING)
            raise
        except BaseException:
            settle(_Outcome.NOTHING)
            raise
        else:
            settle(_Outcome.NOTHING if verdict.untested else _Outcome.SUCCESS)

    def _settle_probe(self, outcome: _Outcome) -> None:
        """Apply the probe's outcome: close on success, re-open at once on failure, free the slot on nothing."""
        self._probe_in_flight = False
        if outcome is _Outcome.SUCCESS:
            self._state = BreakerState.CLOSED
            self._failures = 0
        elif outcome is _Outcome.FAILURE:
            # The host was given its one chance.
            self._open()

    def _settle_call(self, outcome: _Outcome) -> None:
        """Apply a call admitted while closed; a stale one finishing after the trip must not touch the probe."""
        if self._state is not BreakerState.CLOSED:
            return
        if outcome is _Outcome.SUCCESS:
            self._failures = 0
        elif outcome is _Outcome.FAILURE:
            self._failures += 1
            if self._failures >= self._failure_threshold:
                self._open()

    def _open(self) -> None:
        """Trip the breaker, starting its cooldown now."""
        self._state = BreakerState.OPEN
        self._opened_at = self._clock()
