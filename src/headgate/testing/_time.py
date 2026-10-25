# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""Time for tests: a clock that moves only when told to, and retries that do not wait.

Both plug into :func:`headgate._core.timing_override`, which sets the clock and sleep of the clients constructed inside
a block, so they reach a client class that fixes its own options in ``super().__init__()``.
"""

from __future__ import annotations

import asyncio
import math
from contextlib import contextmanager
from dataclasses import replace
from typing import TYPE_CHECKING, Self

from headgate._core import Timing, current_timing, timing_override

if TYPE_CHECKING:
    from collections.abc import Generator

__all__ = ["TestClock", "instant_retries"]


class TestClock:
    """A monotonic clock a test drives: it reads :attr:`now`, and its :meth:`sleep` advances it instead of waiting.

    Build clients under :meth:`install` and every duration they measure (events, deadlines, breaker cooldowns read
    from a registry built by :func:`~headgate.testing.isolated_breakers` in the same block) is on this clock, and every
    wait they make (retry backoff) moves it, so a test of time runs in no time. :attr:`sleeps` records each wait.
    """

    # Not a test class, whatever pytest's name-based collection thinks of "Test...".
    __test__ = False

    def __init__(self, start: float = 0.0) -> None:
        """Start the clock at *start*."""
        self.now = start
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        """Return the current reading."""
        return self.now

    def advance(self, seconds: float) -> None:
        """Move the clock forward by *seconds*; ``ValueError`` for a negative or NaN step, which no clock takes."""
        if not seconds >= 0 or math.isinf(seconds):
            msg = f"a clock only moves forward by a finite step (got {seconds})"
            raise ValueError(msg)
        self.now += seconds

    async def sleep(self, seconds: float, /) -> None:
        """Record *seconds*, advance the clock by them, and yield to the event loop once."""
        self.sleeps.append(seconds)
        self.advance(seconds)
        await asyncio.sleep(0)

    @property
    def timing(self) -> Timing:
        """This clock and its sleep, as the :class:`~headgate._core.Timing` a client takes."""
        return Timing(clock=self, sleep=self.sleep)

    @contextmanager
    def install(self) -> Generator[Self]:
        """Build the clients constructed inside the block on this clock; yield the clock."""
        with timing_override(self.timing):
            yield self


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
