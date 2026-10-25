# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""The rate budget: pacing, a concurrency limit, server feedback and adaptive throttling, in one shareable object.

**Pacing** is GCRA (the generic cell rate algorithm), the virtual-scheduling form of a leaky bucket: the budget keeps
the theoretical arrival time of the next request, and each request reserves its send time against it under a lock,
then sleeps until then. Up to ``rate.burst`` requests go back to back; after that they are ``rate.interval`` apart.
Reservations are made in arrival order, so waiters are served FIFO. A caller cancelled while it waits gives its
reservation back when nobody has reserved after it, so a deadline expiring in the queue does not waste a send slot.

**Sharing.** Passing one budget to several clients (or rules) makes them spend from it together. The state is plain
numbers under a ``threading.Lock`` and every wait goes through the budget's *sleep*, so nothing in it is bound to an
event loop: clients on different loops, in different threads, share it as one budget. The concurrency limit is built
the same way (:class:`~headgate.ratelimit._inflight.InFlightLimit`).

**Server feedback** (:mod:`headgate.ratelimit._feedback`): a response's ``Retry-After`` on a 429/503, or rate-limit
headers saying the quota is spent, pause the whole budget until the reset, so every concurrent and later call queues
behind the pause instead of spending a send on a certain refusal. A call whose deadline would pass before the pause
ends does not wait: it raises :class:`BudgetPausedError` at once, with the seconds left as ``retry_after``.

**Adaptive throttling** (:class:`~headgate.ratelimit.Adaptive`, on by default) scales the pacing rate down on pushback
and back up on success.
"""

from __future__ import annotations

import math
import threading
import time
from typing import TYPE_CHECKING, Final

import httpx2

from headgate._core import SYSTEM_TIMING, RateLimited
from headgate.ratelimit._feedback import read_feedback
from headgate.ratelimit._inflight import InFlightLimit
from headgate.ratelimit._rate import Adaptive, Rate

if TYPE_CHECKING:
    from collections.abc import Callable

    from headgate._core import CallContext, Clock, Timing
    from headgate.ratelimit._feedback import Feedback

__all__ = ["DEFAULT_ADAPTIVE", "PACED", "PAUSED", "PUSHBACK", "THROTTLED", "BudgetPausedError", "RateBudget"]

#: Adaptive throttling with its default factors: what a budget gets unless it is given ``adaptive=None``.
DEFAULT_ADAPTIVE: Final = Adaptive()

#: The ``reason`` of a :class:`~headgate._core.RateLimited` event: pacing at the configured rate, pacing at a rate
#: adaptive throttling lowered, waiting out a pause the server asked for, and a response asking for that pause.
PACED: Final = "paced"
THROTTLED: Final = "throttled"
PAUSED: Final = "paused"
PUSHBACK: Final = "pushback"


class BudgetPausedError(Exception):
    """Raised by :meth:`RateBudget.wait` when the server's pause outlasts the call's deadline.

    ``AsyncBaseClient`` raises it to its caller as :class:`headgate.errors.RateLimitError`. ``retry_after`` is the
    seconds left in the pause; ``status_code`` is the status of the response that asked for it.
    """

    def __init__(self, message: str, *, retry_after: float, status_code: int) -> None:
        """Record *message*, *retry_after* and *status_code*."""
        super().__init__(message)
        self.retry_after = retry_after
        self.status_code = status_code


def _release_nothing() -> None:
    """Release no slot: what :meth:`RateBudget.occupy` returns for a budget without a concurrency limit."""


class RateBudget:
    """A shareable budget: pacing at ``rate``, at most ``max_in_flight`` responses open, and server feedback.

    See the module docstring for the algorithm and for what sharing means. *rate* is the configured rate;
    *max_in_flight* caps how many requests may be in flight at once, a request counting from its send until its
    response body is closed (``None``: no cap); *adaptive* lowers the rate on pushback (``None``: never). *timing*'s
    clock (monotonic seconds) and sleep pace requests and wait out pauses; *wall_clock* is Unix time, for the absolute
    times some rate-limit headers carry.
    """

    __slots__ = (
        "_adaptive",
        "_adjusted_at",
        "_clock",
        "_decreased_at",
        "_factor",
        "_in_flight",
        "_lock",
        "_pause_status",
        "_paused_until",
        "_rate",
        "_sleep",
        "_tat",
        "_wall_clock",
    )

    def __init__(
        self,
        rate: Rate,
        *,
        max_in_flight: int | None = None,
        adaptive: Adaptive | None = DEFAULT_ADAPTIVE,
        timing: Timing = SYSTEM_TIMING,
        wall_clock: Clock = time.time,
    ) -> None:
        """Build a budget; see the class docstring for the parameters.

        :raises ValueError: for a *max_in_flight* below one.
        """
        if max_in_flight is not None and max_in_flight < 1:
            msg = f"max_in_flight must be >= 1, or None (got {max_in_flight})"
            raise ValueError(msg)
        self._rate = rate
        self._adaptive = adaptive
        self._in_flight = None if max_in_flight is None else InFlightLimit(max_in_flight)
        self._clock = timing.clock
        self._sleep = timing.sleep
        self._wall_clock = wall_clock
        self._lock = threading.Lock()
        self._tat = -math.inf
        self._factor = 1.0
        self._adjusted_at = -math.inf
        self._decreased_at = -math.inf
        self._paused_until = -math.inf
        self._pause_status = 0

    @property
    def rate(self) -> Rate:
        """The configured rate."""
        return self._rate

    @property
    def adaptive(self) -> Adaptive | None:
        """The adaptive throttling, or ``None``."""
        return self._adaptive

    @property
    def max_in_flight(self) -> int | None:
        """The concurrency cap, or ``None``."""
        return None if self._in_flight is None else self._in_flight.limit

    @property
    def in_flight(self) -> int:
        """How many requests hold a concurrency slot now (0 without a cap)."""
        return 0 if self._in_flight is None else self._in_flight.in_flight

    @property
    def effective_rate(self) -> Rate:
        """The rate pacing runs at now: the configured rate, scaled down by adaptive throttling."""
        with self._lock:
            return self._current_rate()

    def pause_remaining(self) -> float:
        """Return the seconds left in a pause the server asked for, or 0."""
        with self._lock:
            return max(0.0, self._paused_until - self._clock())

    def now(self) -> float:
        """Return the budget's clock reading, which :meth:`observe` compares send times on."""
        return self._clock()

    # Spending.

    async def wait(self, context: CallContext) -> None:
        """Wait until *context*'s call may send: any server pause, then its paced turn.

        Emits :class:`~headgate._core.RateLimited` for every wait. :class:`RateLimitTransport` calls it once per send.

        :raises BudgetPausedError: when a pause would outlast the call's deadline.
        """
        reservation: tuple[float, float] | None = None
        while True:
            with self._lock:
                now = self._clock()
                pause = self._paused_until - now
                if pause > 0:
                    remaining = context.remaining()
                    if remaining is not None and pause > remaining:
                        self._refund(reservation)
                        raise self._paused_error(pause)
                    delay, reason = pause, PAUSED
                elif reservation is not None:
                    return
                else:
                    reservation, delay = self._reserve(now)
                    reason = THROTTLED if self._factor < 1 else PACED
                    if delay <= 0:
                        return
            if context.observed:
                context.emit(RateLimited(call=context.info, delay=delay, reason=reason))
            try:
                await self._sleep(delay)
            except BaseException:
                with self._lock:
                    self._refund(reservation)
                raise

    async def occupy(self) -> Callable[[], None]:
        """Take a concurrency slot, waiting for one; return the function that gives it back (safe to call twice)."""
        limit = self._in_flight
        if limit is None:
            return _release_nothing
        await limit.acquire()
        released = False
        lock = threading.Lock()

        def release() -> None:
            nonlocal released
            with lock:
                if released:
                    return
                released = True
            limit.release()

        return release

    def observe(self, response: httpx2.Response, *, sent_at: float) -> float | None:
        """Apply what *response* says: pause for the reset it names, and throttle on pushback or recover on success.

        *sent_at* is the budget's clock when the request was sent: a response to a request sent before the last
        decrease does not change the rate again. Returns the pause *response* asked for, in seconds, or ``None``
        when it asked for none; a shorter pause than one already running does not shorten it.
        """
        feedback = read_feedback(response, now=self._wall_clock())
        with self._lock:
            now = self._clock()
            if feedback.pause is not None and now + feedback.pause > self._paused_until:
                self._paused_until = now + feedback.pause
                self._pause_status = response.status_code
            self._adapt(response, feedback, now=now, sent_at=sent_at)
        return feedback.pause

    # Internals, called with the lock held.

    def _adapt(self, response: httpx2.Response, feedback: Feedback, *, now: float, sent_at: float) -> None:
        """Throttle on pushback, or recover toward the configured rate on a success, under adaptive throttling."""
        adaptive = self._adaptive
        if adaptive is None or sent_at < self._decreased_at:
            return
        if feedback.pushback:
            self._factor = max(adaptive.min_rate, self._factor * adaptive.decrease)
            self._decreased_at = self._adjusted_at = now
        elif response.status_code < httpx2.codes.INTERNAL_SERVER_ERROR:
            restored = adaptive.increase * (now - self._adjusted_at)
            self._factor = min(1.0, self._factor + restored)
            self._adjusted_at = now

    def _current_rate(self) -> Rate:
        """Return the rate pacing runs at now: the configured one scaled by the adaptive factor (1 when unthrottled)."""
        return Rate(requests=self._rate.requests * self._factor, period=self._rate.period)

    def _reserve(self, now: float) -> tuple[tuple[float, float], float]:
        """Reserve the next send: return the reservation (the arrival time before and after it) and the wait."""
        rate = self._current_rate()
        interval = rate.interval
        tolerance = (rate.burst - 1) * interval
        arrival = max(self._tat, now)
        self._tat = arrival + interval
        return (arrival, self._tat), max(0.0, arrival - tolerance - now)

    def _refund(self, reservation: tuple[float, float] | None) -> None:
        """Give *reservation* back when it is still the latest one, so the next caller takes its place."""
        if reservation is not None and reservation[1] == self._tat:
            self._tat = reservation[0]

    def _paused_error(self, pause: float) -> BudgetPausedError:
        """Build the error for a pause of *pause* seconds that outlasts a call's deadline."""
        msg = f"rate budget paused for {pause:.3g}s at the server's request, past the call's deadline"
        return BudgetPausedError(msg, retry_after=pause, status_code=self._pause_status)
