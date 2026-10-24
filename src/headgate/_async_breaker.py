"""The per-host circuit breaker: stop calling a host that is failing, find out cheaply when it recovers.

One breaker per HOST, shared by every client in the process through :data:`_host_breakers`,
so two clients addressing the same upstream cannot each spend their own quota of failures
against it.

**Why this is not the ``circuitbreaker`` library any more.** That package was
driven by a ``with breaker:`` block wrapped around the transport, and three things followed
from it that were wrong rather than merely different:

* its ``__exit__`` records a failure only for an ``Exception`` and calls ``reset()`` for
  anything else — so an ``asyncio.CancelledError``, which is what every ``asyncio.wait_for``
  around a client call raises when the caller's own timeout is shorter than the client's,
  CLEARED the failure count. A hanging host could never trip the breaker.
* half-open admitted every waiting caller at once, so a host that had just failed got the
  full concurrent load as its recovery test.
* wrapped around the transport it saw the response HEADERS, because the body is read
  afterwards (``_send`` streams so it can cap the size). A connection dying mid-body was
  invisible to it.

The cure for the third is placement, not code: the breaker is driven from ``_send``, around
the send AND the body read, which is also the only place that knows the whole fetch
succeeded. That is why there is no breaker TRANSPORT any more.

**What is deliberately unchanged:** the trip rule is still five CONSECUTIVE failures, so any
success resets the count. A host failing half its calls therefore never trips — that is the
classic consecutive-failure semantics, a policy question rather than a defect, and changing
it changes when production stops calling a provider. A failure-rate window over the last N
calls is the obvious alternative if that is ever wanted.
"""

from __future__ import annotations

import time
from collections import defaultdict
from contextlib import contextmanager
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol, override

if TYPE_CHECKING:
    from collections.abc import Generator
    from contextlib import AbstractContextManager

#: The registry every client shares, keyed by host. Module-level so concurrent clients to
#: one host share a breaker, and so tests can clear or inspect it.
_host_breakers: defaultdict[str, AsyncCircuitBreaker]


class BreakerState(StrEnum):
    """Circuit-breaker states; the value is what :attr:`AsyncCircuitBreaker.state` reports."""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitOpenError(Exception):
    """Raised when a call is refused because the breaker is open (or already probing)."""


class Verdict:
    """What one fetch established about its host, marked by ``_base._send`` inside :meth:`AsyncCircuitBreaker.track`.

    Unmarked means "nothing": only an explicit mark makes a raise a host failure, and only a clean exit that was not
    marked neutral is a host success. See :meth:`AsyncCircuitBreaker.track` for why the default is not the reverse.
    """

    __slots__ = ("failed", "neutral")

    def __init__(self) -> None:
        self.failed = False
        self.neutral = False

    def host_failed(self) -> None:
        """The wire failed: a transport error or timeout, or a 429/5xx that survived retries."""
        self.failed = True

    def not_a_host_test(self) -> None:
        """The fetch reached no network (a cache hit), so its success says nothing about the host."""
        self.neutral = True


class AsyncCircuitBreaker:
    """CLOSED / OPEN / HALF_OPEN breaker driven by a ``with breaker.track()`` block.

    No lock: every state change happens between awaits, in a single-threaded event loop, so
    there is no critical section to protect — and a ``asyncio.Lock`` here would be actively
    wrong, since it binds to the loop that first awaited it while this registry is
    process-global and outlives any one loop (pytest gives each test its own).
    """

    __slots__ = (
        "_failure_threshold",
        "_failures",
        "_name",
        "_opened_at",
        "_probe_in_flight",
        "_recovery_timeout",
        "_state",
    )

    def __init__(self, *, failure_threshold: int = 5, recovery_timeout: float = 60.0, name: str = "") -> None:
        self._failure_threshold = failure_threshold
        self._recovery_timeout = recovery_timeout
        self._name = name
        self._state = BreakerState.CLOSED
        self._failures = 0
        self._opened_at = 0.0
        self._probe_in_flight = False

    @property
    def state(self) -> BreakerState:
        """The current state. OPEN whose cooldown has elapsed still reads OPEN until a probe is taken."""
        return self._state

    @property
    def refusing(self) -> bool:
        """Whether a call made now would be refused: OPEN inside its cooldown, or a probe already out.

        Unlike :attr:`state`, this goes false once the cooldown elapses, so a host nothing has called since it
        tripped does not read as refusing forever.
        """
        return not self._admits()

    @property
    def failure_count(self) -> int:
        """Consecutive failures recorded since the last success."""
        return self._failures

    def _admits(self) -> bool:
        """Whether a call would be let through, WITHOUT taking the probe slot."""
        if self._state is BreakerState.CLOSED:
            return True
        if self._probe_in_flight:
            return False
        return time.monotonic() - self._opened_at >= self._recovery_timeout

    @contextmanager
    def track(self) -> Generator[Verdict]:
        """Run one fetch through the breaker.

        Raises :class:`CircuitOpenError` instead of yielding when the host is being refused. Otherwise yields a
        :class:`Verdict` the fetch marks as it goes, and what the call records on the way out is:

        * **a host FAILURE** — only when the fetch marked :meth:`Verdict.host_failed` before raising, which
          ``_base._send`` does for a transport error or timeout and for a 429/5xx that survived retries. Those are
          what the WIRE said about this host;
        * **a SUCCESS** — a clean exit, unless the fetch marked :meth:`Verdict.not_a_host_test` (a cache hit, which
          reached no network). A success resets the consecutive-failure count;
        * **NOTHING** — everything else: an exception the fetch did not mark (an auth flow failing to mint a token, a
          body over the size cap, a programming error) and a ``BaseException`` such as ``CancelledError`` (the
          caller gave up). None of those says whether the host is up.

        Previously every ``Exception`` counted and every clean exit was a success. So five token mints refused
        for a CONFIGURATION reason opened the breaker for every client on that host, hiding the real error behind
        ``circuit breaker open``, and a cache hit reset the failure count and could close a probe that never reached
        the host.

        **Only the call that took the probe slot settles a probe.** A call admitted while the breaker was CLOSED that
        finishes after it tripped — realistic, since three 30s attempts plus backoff outlast the cooldown — changes no
        state at all. Previously its cancellation freed the probe slot, admitting a second concurrent probe, and
        its failure re-opened the breaker while the real probe was still out.
        """
        if not self._admits():
            raise CircuitOpenError(f"circuit breaker open for {self._name}")
        probing = self._state is not BreakerState.CLOSED
        if probing:
            # The cooldown has elapsed and this caller is the canary: take the slot so
            # concurrent callers are refused until it reports.
            self._state = BreakerState.HALF_OPEN
            self._probe_in_flight = True

        verdict = Verdict()
        try:
            yield verdict
        except Exception:
            self._settle(probing=probing, failed=True if verdict.failed else None)
            raise
        except BaseException:
            self._settle(probing=probing, failed=None)
            raise
        else:
            self._settle(probing=probing, failed=None if verdict.neutral else False)

    def _settle(self, *, probing: bool, failed: bool | None) -> None:
        """Apply one call's outcome: ``True`` a host failure, ``False`` a host success, ``None`` nothing learned."""
        if probing:
            self._probe_in_flight = False
            if failed is None:
                # The probe tested nothing; the slot is free again and the next admitted caller takes it.
                return
            if failed:
                # A failed probe re-opens immediately: the host was given its one chance.
                self._state = BreakerState.OPEN
                self._opened_at = time.monotonic()
            else:
                self._state = BreakerState.CLOSED
                self._failures = 0
            return
        if failed is None or self._state is not BreakerState.CLOSED:
            # Nothing learned, or a stale call from before the trip: neither may touch the probe in flight.
            return
        if not failed:
            self._failures = 0
            return
        self._failures += 1
        if self._failures >= self._failure_threshold:
            self._state = BreakerState.OPEN
            self._opened_at = time.monotonic()


class Breaker(Protocol):
    """What a client needs of a breaker: one ``track()`` scope per fetch.

    :class:`AsyncCircuitBreaker` is the real one.
    """

    def track(self) -> AbstractContextManager[Verdict]: ...


class Breakers(Protocol):
    """Where a client looks up its host's breaker: :data:`_host_breakers`, or :data:`NO_BREAKERS`."""

    def __getitem__(self, host: str, /) -> Breaker: ...


class _NoBreaker:
    """A breaker that admits every call and records nothing."""

    __slots__ = ()

    @contextmanager
    def track(self) -> Generator[Verdict]:
        yield Verdict()


class _NoBreakers:
    """The registry of a client built with no circuit breaker (``circuit_breaker=False``, or transport policy off).

    Every host maps to the one :class:`_NoBreaker`. It never reads :data:`_host_breakers`, and that is half of what
    "no breaker" means: the registry is process-global, so a client that merely stopped RECORDING would still be
    refused by a breaker another client opened on the same host.
    """

    __slots__ = ()

    def __getitem__(self, host: str, /) -> _NoBreaker:
        return _NO_BREAKER


_NO_BREAKER = _NoBreaker()

#: The breaker registry for a client with no breaker. See :class:`_NoBreakers`.
NO_BREAKERS = _NoBreakers()


class _BreakerRegistry(defaultdict[str, "AsyncCircuitBreaker"]):
    """A ``defaultdict`` whose factory gets the key, so each breaker knows its own host."""

    @override
    def __missing__(self, host: str) -> AsyncCircuitBreaker:
        breaker = AsyncCircuitBreaker(name=host)
        self[host] = breaker
        return breaker


_host_breakers = _BreakerRegistry()


__all__ = [
    "NO_BREAKERS",
    "AsyncCircuitBreaker",
    "Breaker",
    "BreakerState",
    "Breakers",
    "CircuitOpenError",
    "Verdict",
    "_host_breakers",
]
