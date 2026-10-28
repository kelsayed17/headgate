# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""The rate budget: pacing, a concurrency limit, server feedback and adaptive throttling, in one shareable object.

**Pacing** is GCRA (the generic cell rate algorithm), the virtual-scheduling form of a leaky bucket: the budget keeps
the theoretical arrival time of the next request, and each request reserves its send time against it under a lock,
then sleeps until then. Up to ``rate.burst`` requests go back to back; after that they are ``rate.interval`` apart.
Reservations are made in arrival order, so waiters are served FIFO. A caller cancelled while it waits gives its
reservation back when nobody has reserved after it, so a deadline expiring in the queue does not waste a send slot.
The schedule is kept on its own timeline, which a server pause shifts later (see below), so a pause postpones a
reservation without taking it away.

**Sharing.** Passing one budget to several clients (or rules) makes them spend from it together. The state is plain
numbers under a ``threading.Lock`` and every wait goes through the budget's *sleep*, so nothing in it is bound to an
event loop: clients on different loops, in different threads, share it as one budget. The concurrency limit is built
the same way (:class:`~headgate.ratelimit._inflight.InFlightLimit`).

**Server feedback** (:mod:`headgate.ratelimit._feedback`): a response's ``Retry-After`` on a 429/503, or rate-limit
headers saying the quota is spent, pause the whole budget until the reset, so every concurrent and later call queues
behind the pause instead of spending a send on a certain refusal. A pause lasts at most :data:`MAX_PAUSE` seconds, so
one hostile or mistaken header cannot stop every client sharing the budget for longer. The calls already waiting their
turn when a pause begins are postponed together: the first goes when the pause ends, the rest follow at the rate, in
their order, and a call arriving during the pause queues behind them. A call whose deadline would pass before the pause
ends does not wait: it raises :class:`BudgetPausedError` at once, with the seconds left as ``retry_after``.

**Adaptive throttling** (:class:`~headgate.ratelimit.Adaptive`, on by default) scales the pacing rate down on pushback
and back up on success.
"""

from __future__ import annotations

import math
import threading
import time
import weakref
from typing import TYPE_CHECKING, Final

import httpx2

from headgate._core import SYSTEM_TIMING, BudgetBacklogError, BudgetPausedError, RateLimited
from headgate.ratelimit._feedback import read_feedback
from headgate.ratelimit._inflight import InFlightLimit
from headgate.ratelimit._rate import Adaptive, Rate

if TYPE_CHECKING:
    from collections.abc import Callable

    from headgate._core import CallContext, Clock, Timing
    from headgate.ratelimit._feedback import Feedback

__all__ = [
    "DEFAULT_ADAPTIVE",
    "MAX_PAUSE",
    "PACED",
    "PAUSED",
    "PUSHBACK",
    "THROTTLED",
    "BudgetPausedError",
    "Pacer",
    "RateBudget",
    "pacer_of",
]

#: Adaptive throttling with its default factors: what a budget gets unless it is given ``adaptive=None``.
DEFAULT_ADAPTIVE: Final = Adaptive()

#: The longest pause, in seconds, a response puts on a budget: an hour, the longest quota window in common use
#: (GitHub's, say), so a spent hourly quota still pauses until its reset while a ``Retry-After`` of years cannot stop
#: the budget for good. A longer pause is cut to it.
MAX_PAUSE: Final = 3600.0

#: The ``reason`` of a :class:`~headgate._core.RateLimited` event: pacing at the configured rate, pacing at a rate
#: adaptive throttling lowered, waiting out a pause the server asked for, and a response asking for that pause.
PACED: Final = "paced"
THROTTLED: Final = "throttled"
PAUSED: Final = "paused"
PUSHBACK: Final = "pushback"


def _release_nothing() -> None:
    """Release no slot: what :meth:`Pacer.occupy` returns for a budget without a concurrency limit."""


class _Turn:
    """A waiter's reservation: the arrival times before and after it, and its send time, on the budget's timeline.

    The send is due at ``send`` plus the budget's shift (the seconds pauses have moved its timeline). ``shift`` is the
    shift when the turn was reserved, so a turn a pause postponed reports its wait as the pause's; ``reason`` is the
    reason it reports otherwise. ``slept`` is the shift and the pause's end when the waiter last slept until its send,
    or ``None``.
    """

    __slots__ = ("after", "before", "reason", "send", "shift", "slept")

    def __init__(self, *, before: float, after: float, send: float, shift: float, reason: str) -> None:
        """Record the reservation."""
        self.before = before
        self.after = after
        self.send = send
        self.shift = shift
        self.reason = reason
        self.slept: tuple[float, float] | None = None


class Pacer:
    """The state and the operations behind a :class:`RateBudget`: what the rate-limit layer spends from.

    One pacer per budget, found with :func:`pacer_of`; every client given the same budget spends through it. See the
    module docstring for the algorithm and for what sharing means.
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
        "_shift",
        "_sleep",
        "_tat",
        "_turns",
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
        self._shift = 0.0
        self._turns: set[_Turn] = set()

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
        """Wait until *context*'s call may send: its paced turn, and the end of any server pause.

        The turn is reserved at once, so the call keeps its place in the queue through a pause that begins while it
        waits (see :meth:`observe`). Emits :class:`~headgate._core.RateLimited` for every wait.
        :class:`RateLimitTransport` calls it once per send.

        :raises BudgetPausedError: when a pause would outlast the call's deadline.
        :raises BudgetBacklogError: when the call's turn comes after its deadline.
        """
        with self._lock:
            turn = self._reserve(self._clock())
        try:
            while True:
                with self._lock:
                    delay, reason = self._wait_for(turn, context)
                    if delay <= 0:
                        self._turns.discard(turn)
                        return
                if context.observed:
                    context.emit(RateLimited(call=context.info, delay=delay, reason=reason))
                await self._sleep(delay)
        except BaseException:
            with self._lock:
                self._refund(turn)
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
        decrease does not change the rate again. Returns the pause *response* asked for, in seconds and cut to
        :data:`MAX_PAUSE`, or ``None`` when it asked for none; a shorter pause than one already running does not shorten
        it. Every turn still waiting is postponed so that the first goes when the pause ends, each keeping its
        place and its spacing behind it.
        """
        feedback = read_feedback(response, now=self._wall_clock())
        pause = None if feedback.pause is None else min(feedback.pause, MAX_PAUSE)
        with self._lock:
            now = self._clock()
            if pause is not None and now + pause > self._paused_until:
                self._pause(now + pause, status_code=response.status_code)
            self._adapt(response, feedback, now=now, sent_at=sent_at)
        return pause

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

    def _reserve(self, now: float) -> _Turn:
        """Reserve the next send at *now* and return it, counted among the turns waiting."""
        rate = self._current_rate()
        interval = rate.interval
        # The timeline stands still through a pause: a call arriving during one queues from the pause's end.
        arrival = max(self._tat, max(now, self._paused_until) - self._shift)
        turn = _Turn(
            before=arrival,
            after=arrival + interval,
            send=arrival - (rate.burst - 1) * interval,
            shift=self._shift,
            reason=THROTTLED if self._factor < 1 else PACED,
        )
        self._tat = turn.after
        self._turns.add(turn)
        return turn

    def _wait_for(self, turn: _Turn, context: CallContext) -> tuple[float, str]:
        """Return how long *turn*'s call waits now, and why: a pause first, then the turn itself; 0 when it may send.

        :raises BudgetPausedError: when the pause outlasts the call's deadline.
        :raises BudgetBacklogError: when the turn, later than any pause, comes at or after it.
        """
        timeline = (self._shift, self._paused_until)
        if turn.slept == timeline:
            # The waiter slept until its send and no pause has moved it since: its turn has come, whatever the clock
            # reads after a sleep that ended a hair early.
            return 0.0, turn.reason
        now = self._clock()
        pause = self._paused_until - now
        due = turn.send + self._shift - now
        delay = max(pause, due)
        if delay <= 0:
            return 0.0, turn.reason
        remaining = context.remaining()
        if remaining is not None and pause > remaining:
            raise self._paused_error(pause)
        # A pause may end exactly at the deadline; a turn after the pause must come before it.
        if remaining is not None and due > pause and due >= remaining:
            msg = f"the rate budget's turn in {due:.3g}s comes after the call's deadline"
            raise BudgetBacklogError(msg, retry_after=due)
        if pause > 0:
            return pause, PAUSED
        turn.slept = timeline
        return due, PAUSED if turn.shift != self._shift else turn.reason

    def _pause(self, until: float, *, status_code: int) -> None:
        """Pause until *until*, shifting the timeline so the earliest waiting turn falls due no sooner than that."""
        self._paused_until = until
        self._pause_status = status_code
        if self._turns:
            first = min(turn.send for turn in self._turns) + self._shift
            self._shift += max(0.0, until - first)

    def _refund(self, turn: _Turn) -> None:
        """Drop *turn* from the waiting, and give it back when it is still the latest, so the next caller takes it."""
        self._turns.discard(turn)
        if turn.after == self._tat:
            self._tat = turn.before

    def _paused_error(self, pause: float) -> BudgetPausedError:
        """Build the error for a pause of *pause* seconds that outlasts a call's deadline."""
        msg = f"rate budget paused for {pause:.3g}s at the server's request, past the call's deadline"
        return BudgetPausedError(msg, retry_after=pause, status_code=self._pause_status)


class RateBudget:
    """A shareable budget: pacing at ``rate``, at most ``max_in_flight`` responses open, and server feedback.

    See the module docstring for the algorithm and for what sharing means. *rate* is the configured rate;
    *max_in_flight* caps how many requests may be in flight at once, a request counting from its turn until its
    response body is closed (``None``: no cap); *adaptive* lowers the rate on pushback (``None``: never). *timing*'s
    clock (monotonic seconds) and sleep pace requests and wait out pauses; *wall_clock* is Unix time, for the absolute
    times some rate-limit headers carry.

    Two clients given the same budget pace together. What it reports (its rate, its effective rate after adaptive
    throttling, how many requests are in flight, how long a server pause has left) is read here; what spends from it is
    the client's own.

    :raises ValueError: for a *max_in_flight* below one.
    """

    __slots__ = ("__weakref__", "_pacer")

    def __init__(
        self,
        rate: Rate,
        *,
        max_in_flight: int | None = None,
        adaptive: Adaptive | None = DEFAULT_ADAPTIVE,
        timing: Timing = SYSTEM_TIMING,
        wall_clock: Clock = time.time,
    ) -> None:
        """Build a budget and the pacer it spends through; see the class docstring for the parameters."""
        self._pacer = Pacer(rate, max_in_flight=max_in_flight, adaptive=adaptive, timing=timing, wall_clock=wall_clock)
        _PACERS[self] = self._pacer

    @property
    def rate(self) -> Rate:
        """The configured rate."""
        return self._pacer.rate

    @property
    def adaptive(self) -> Adaptive | None:
        """How the rate adapts to pushback, or ``None`` when it does not."""
        return self._pacer.adaptive

    @property
    def max_in_flight(self) -> int | None:
        """How many requests may be in flight at once, or ``None`` for no cap."""
        return self._pacer.max_in_flight

    @property
    def in_flight(self) -> int:
        """How many requests are in flight now."""
        return self._pacer.in_flight

    @property
    def effective_rate(self) -> Rate:
        """The rate pacing runs at now: the configured rate, scaled down by adaptive throttling."""
        return self._pacer.effective_rate

    @property
    def pause_remaining(self) -> float:
        """The seconds left in a pause the server asked for, or 0."""
        return self._pacer.pause_remaining()


#: Each live budget's pacer. Weak, so a budget nobody holds any more goes with its pacer.
_PACERS: Final[weakref.WeakKeyDictionary[RateBudget, Pacer]] = weakref.WeakKeyDictionary()


def pacer_of(budget: RateBudget) -> Pacer:
    """Return the pacer *budget* spends through."""
    return _PACERS[budget]
