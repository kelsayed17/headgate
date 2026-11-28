# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""Time and randomness as parameters: the clocks, the sleep and the random source every layer is handed.

Each layer that waits, measures or draws takes them as one :class:`Timing`, so a test drives time instead of waiting
for it, and pins a draw instead of hoping. The shapes are protocols over plain callables, so ``time.monotonic``,
``time.time``, ``asyncio.sleep`` and ``random.random`` satisfy them as they are.

A client (and the token auth it is given), and a shared budget or registry, read :func:`current_timing` when they are
built. An SDK usually fixes its client's options and exposes none of them, so a
test replaces the timing of everything it builds with :func:`timing_override` instead of reaching into each one.
"""

import asyncio
import random
import time
from collections.abc import Awaitable, Generator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Protocol, final

from headgate._core._kinds import check_callable, check_kind, sealed

__all__ = [
    "SYSTEM_TIMING",
    "Clock",
    "RandomSource",
    "Sleep",
    "Timing",
    "asyncio_sleep",
    "current_timing",
    "system_random",
    "timing_override",
]


class Clock(Protocol):
    """Return monotonic seconds: only differences between two readings mean anything."""

    def __call__(self) -> float:
        """Return the current reading."""
        ...


class Sleep(Protocol):
    """Wait *seconds* without blocking the event loop."""

    def __call__(self, seconds: float, /) -> Awaitable[None]:
        """Return an awaitable that completes after *seconds*."""
        ...


class RandomSource(Protocol):
    """Return a uniform draw in ``[0, 1)``."""

    def __call__(self) -> float:
        """Return the next draw."""
        ...


async def asyncio_sleep(seconds: float, /) -> None:
    """Sleep through ``asyncio.sleep``, looked up on every call so a process-wide patch of it still applies."""
    await asyncio.sleep(seconds)


#: The operating system's generator, not the ``random`` module's shared one: an application that seeds ``random`` (for
#: reproducible tests, say) would otherwise give every process it runs the same jitter, which exists to spread them.
system_random: RandomSource = random.SystemRandom().random


@final
@sealed("build a Timing of the clock, sleep, wall_clock and random you need")
@dataclass(frozen=True, slots=True, kw_only=True)
class Timing:
    """The clocks, the sleep and the random source a client, a token auth, a budget or a registry runs on.

    * ``clock``: monotonic seconds, which every duration is measured on: deadlines, pacing, cooldowns, token lifetimes.
    * ``sleep``: how a wait is waited.
    * ``wall_clock``: Unix seconds, which a ``Retry-After`` date and a ``RateLimit`` epoch are read against.
    * ``random``: the draw a backoff's jitter is made from.

    :raises TypeError: for a field that cannot be called.
    """

    clock: Clock = field(default=time.monotonic)
    sleep: Sleep = field(default=asyncio_sleep)
    wall_clock: Clock = field(default=time.time)
    random: RandomSource = field(default=system_random)

    def __post_init__(self) -> None:
        """Refuse a clock, a sleep or a random source that cannot be called."""
        check_callable("clock", self.clock, "a function returning monotonic seconds, such as time.monotonic")
        check_callable("sleep", self.sleep, "an async function of seconds, such as asyncio.sleep")
        check_callable("wall_clock", self.wall_clock, "a function returning Unix seconds, such as time.time")
        check_callable("random", self.random, "a function returning a draw in [0, 1), such as random.random")


#: Production timing: ``time.monotonic``, ``asyncio.sleep``, ``time.time`` and the operating system's random draw.
SYSTEM_TIMING = Timing()

#: The timing clients built in this context take. A ``ContextVar`` so concurrent tasks cannot see each other's override.
_TIMING: ContextVar[Timing] = ContextVar("headgate_timing", default=SYSTEM_TIMING)


def current_timing() -> Timing:
    """Return the timing an object built right now takes."""
    return _TIMING.get()


@contextmanager
def timing_override(timing: Timing) -> Generator[None]:
    """Build with *timing*, not the system's (``Timing()``), everything constructed inside the block with none given.

    :raises TypeError: for a *timing* that is not a :class:`Timing`, when the block is entered.
    """
    check_kind("timing", timing, Timing, "a Timing, such as TestClock().timing")
    token = _TIMING.set(timing)
    try:
        yield
    finally:
        _TIMING.reset(token)
