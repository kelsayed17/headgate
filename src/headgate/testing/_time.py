# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""Time for tests: a clock that moves only when told to, and retries that do not wait.

Both plug into :func:`headgate._core.timing_override`, which sets the clock and sleep of the clients constructed inside
a block, so they reach a client class that fixes its own options in ``super().__init__()``.
"""

from __future__ import annotations

import asyncio
import heapq
import itertools
import math
from contextlib import contextmanager
from dataclasses import replace
from typing import TYPE_CHECKING, Final, Self

from headgate._core import Timing, current_timing, timing_override

if TYPE_CHECKING:
    from collections.abc import Generator

__all__ = ["TestClock", "instant_retries"]

#: How many turns of the event loop pass with no sleep beginning or ending before the clock moves to the next wake:
#: room for the tasks that are not sleeping, and for those just woken, to run until they wait again.
_SETTLE_TURNS: Final = 16


class TestClock:
    """A monotonic clock a test drives: it reads :attr:`now`, and its :meth:`sleep` advances it instead of waiting.

    Build clients under :meth:`install` and every duration they measure (events, deadlines, breaker cooldowns read
    from a registry built by :func:`~headgate.testing.isolated_breakers` in the same block) is on this clock, and every
    wait they make (retry backoff) moves it, so a test of time runs in no time. :attr:`sleeps` records each wait.

    Concurrent sleeps overlap, as they do in real time: each sleeper wakes at the reading it began at plus its delay.
    Once the event loop has turned a few times with no sleep beginning or ending, so that every task not sleeping has
    reached its next wait, the clock moves to the earliest wake. Two calls backing off 30 seconds together both retry
    at 30, not one at 30 and the other at 60.
    """

    # Not a test class, whatever pytest's name-based collection thinks of "Test...".
    __test__ = False

    def __init__(self, start: float = 0.0) -> None:
        """Start the clock at *start*."""
        self.now = start
        self.sleeps: list[float] = []
        self._sleepers: list[tuple[float, int, asyncio.Future[None]]] = []
        self._order = itertools.count()
        self._driven_on: asyncio.AbstractEventLoop | None = None
        self._quiet_turns = 0

    def __call__(self) -> float:
        """Return the current reading."""
        return self.now

    def advance(self, seconds: float) -> None:
        """Move the clock forward by *seconds*; ``ValueError`` for a negative or NaN step, which no clock takes."""
        _check_step(seconds)
        self.now += seconds

    async def sleep(self, seconds: float, /) -> None:
        """Record *seconds* and return once the clock reads *seconds* past now, moving it there if nothing wakes first.

        A sleep cancelled before it wakes leaves the clock where it was. ``ValueError`` for a step :meth:`advance`
        refuses.
        """
        _check_step(seconds)
        self.sleeps.append(seconds)
        loop = asyncio.get_running_loop()
        wake = loop.create_future()
        heapq.heappush(self._sleepers, (self.now + seconds, next(self._order), wake))
        self._drive(loop)
        await wake

    def _drive(self, loop: asyncio.AbstractEventLoop) -> None:
        """Restart the count of quiet turns, and keep the clock's turns going on *loop*."""
        self._quiet_turns = 0
        if self._driven_on is not loop:
            self._driven_on = loop
            loop.call_soon(self._turn, loop)

    def _turn(self, loop: asyncio.AbstractEventLoop) -> None:
        """Take one turn of *loop*: after enough quiet ones, move to the earliest wake and wake every sleeper due."""
        sleepers = self._sleepers
        while sleepers and (sleepers[0][2].done() or sleepers[0][2].get_loop() is not loop):
            heapq.heappop(sleepers)  # cancelled, or left behind by a loop that has closed
        if not sleepers:
            self._driven_on = None
            return
        if self._quiet_turns < _SETTLE_TURNS:
            self._quiet_turns += 1
        else:
            self.now = max(self.now, sleepers[0][0])
            while sleepers and sleepers[0][0] <= self.now:
                _, _, wake = heapq.heappop(sleepers)
                if not wake.done():
                    wake.set_result(None)
            self._quiet_turns = 0
        loop.call_soon(self._turn, loop)

    @property
    def timing(self) -> Timing:
        """This clock and its sleep, as the :class:`~headgate._core.Timing` a client takes."""
        return Timing(clock=self, sleep=self.sleep)

    @contextmanager
    def install(self) -> Generator[Self]:
        """Build the clients constructed inside the block on this clock; yield the clock."""
        with timing_override(self.timing):
            yield self


def _check_step(seconds: float) -> None:
    """Raise ``ValueError`` for a negative, NaN or infinite step, which no clock takes."""
    if not seconds >= 0 or math.isinf(seconds):
        msg = f"a clock only moves forward by a finite step (got {seconds})"
        raise ValueError(msg)


async def _yield_once(_seconds: float, /) -> None:
    """Yield to the event loop once instead of sleeping, as a zero-second backoff would."""
    await asyncio.sleep(0)


@contextmanager
def instant_retries() -> Generator[None]:
    """Build the clients constructed inside the block with a sleep that returns at once.

    A mocked failure is then retried exactly as production retries it, without waiting through the backoff. The clock
    is left as it is, so this composes with :meth:`TestClock.install` (whose sleep it replaces).
    """
    with timing_override(replace(current_timing(), sleep=_yield_once)):
        yield
