# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""The per-host circuit breaker: stop calling a host that is failing, find out cheaply when it recovers.

There is one breaker per host, shared by every client in the process through :data:`HOST_BREAKERS`, so two clients
addressing the same upstream cannot each spend their own quota of failures against it.

Three design choices, each fixing a failure mode of the usual ``with breaker:`` wrapper:

* **Only a marked failure counts.** An ``asyncio.CancelledError`` (what ``asyncio.wait_for`` raises around a call when
  the caller's timeout is shorter than the client's) records nothing, rather than clearing the failure count; so does
  an auth flow failing to mint or a body over the size cap. See :meth:`CircuitBreaker.track`.
* **Half-open admits one probe.** A host that has just failed gets one call as its recovery test, not the full
  concurrent load.
* **The breaker spans the body read.** ``AsyncBaseClient`` drives it around the send and the body read, so a connection
  dying mid-body is a failure of the fetch. A transport layer would see only the response headers, which is why there
  is no breaker transport.

The trip rule is five consecutive failures, so any success resets the count, and a host failing half its calls never
trips. A failure-rate window over the last N calls is the obvious alternative if that is ever wanted.

There is no lock: every state change happens between awaits on one event loop, so there is no critical section. An
``asyncio.Lock`` would be wrong here besides, since it binds to the loop that first awaits it while the registry is
process-global and outlives any one loop.
"""

from __future__ import annotations

import enum
import time
from contextlib import contextmanager
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Callable, Generator
    from contextlib import AbstractContextManager

__all__ = [
    "HOST_BREAKERS",
    "NO_BREAKERS",
    "Breaker",
    "BreakerState",
    "Breakers",
    "CircuitBreaker",
    "CircuitOpenError",
    "Verdict",
]


class BreakerState(StrEnum):
    """Circuit-breaker states; the value is what :attr:`CircuitBreaker.state` reports."""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitOpenError(Exception):
    """Raised when a call is refused because the breaker is open, or its one probe is already out."""


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
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Trip after *failure_threshold* consecutive failures; admit a probe *recovery_timeout* seconds later.

        *name* (the host) appears in the :class:`CircuitOpenError` message. *clock* returns monotonic seconds; it is a
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

        Raises :class:`CircuitOpenError` instead of yielding when the host is being refused. Otherwise yields a
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
            raise CircuitOpenError(msg)
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


class Breaker(Protocol):
    """What a client needs of a breaker: one ``track()`` scope per fetch; :class:`CircuitBreaker` is the real one."""

    def track(self) -> AbstractContextManager[Verdict]:
        """Run one fetch through the breaker, yielding the :class:`Verdict` it marks."""
        ...


class Breakers(Protocol):
    """Where a client looks up its host's breaker: :data:`HOST_BREAKERS`, or :data:`NO_BREAKERS`."""

    def __getitem__(self, host: str, /) -> Breaker:
        """Return *host*'s breaker."""
        ...


class _NoBreaker:
    """A breaker that admits every call and records nothing."""

    __slots__ = ()

    @contextmanager
    def track(self) -> Generator[Verdict]:
        """Admit the call; the verdict it marks is discarded."""
        yield Verdict()


class _NoBreakers:
    """Map every host to a breaker that admits all calls and records nothing.

    The registry of a client built with ``TransportPolicy(circuit_breaker=False)``, or with transport policy off. It
    never reads :data:`HOST_BREAKERS`, and that is half of what
    "no breaker" means: the registry is process-global, so a client that merely stopped recording would still be
    refused by a breaker another client opened on the same host.
    """

    __slots__ = ()

    def __getitem__(self, host: str, /) -> _NoBreaker:
        """Return the shared no-op breaker, whatever the host."""
        return _NO_BREAKER


_NO_BREAKER = _NoBreaker()

#: The breaker registry for a client with no breaker. See :class:`_NoBreakers`.
NO_BREAKERS = _NoBreakers()


class _BreakerRegistry(dict[str, CircuitBreaker]):
    """A host-to-breaker mapping that builds a missing host's breaker on lookup, named for its host."""

    def __missing__(self, host: str) -> CircuitBreaker:
        """Build, register and return *host*'s breaker (``dict.__getitem__`` calls this for a missing key)."""
        breaker = self[host] = CircuitBreaker(name=host)
        return breaker


#: The registry every client shares, keyed by host. Module-level so concurrent clients to one host share a breaker,
#: and so tests can clear or inspect it.
HOST_BREAKERS = _BreakerRegistry()
