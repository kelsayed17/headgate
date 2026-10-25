# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""Time and randomness as parameters: the clock, the sleep and the random source every layer is handed.

Each layer that waits, measures or draws takes the function it needs as a parameter with a production default, so a
test drives time instead of waiting for it. The three shapes are protocols over plain callables, so ``time.monotonic``,
``asyncio.sleep`` and ``random.random`` satisfy them as they are.

``AsyncBaseClient`` reads the :class:`Timing` it hands its layers from :func:`current_timing` when it is built. A client
class usually fixes its own options in ``super().__init__()`` and exposes none of them, so a test replaces the timing
of the clients it builds with :func:`timing_override` instead of reaching into each one.
"""

from __future__ import annotations

import asyncio
import random
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Awaitable, Generator

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


@dataclass(frozen=True, slots=True, kw_only=True)
class Timing:
    """The clock and the sleep a client hands every layer it builds."""

    clock: Clock = field(default=time.monotonic)
    sleep: Sleep = field(default=asyncio_sleep)


#: Production timing: ``time.monotonic`` and ``asyncio.sleep``.
SYSTEM_TIMING = Timing()

#: The timing clients built in this context take. A ``ContextVar`` so concurrent tasks cannot see each other's override.
_TIMING: ContextVar[Timing] = ContextVar("headgate_timing", default=SYSTEM_TIMING)


def current_timing() -> Timing:
    """Return the timing a client constructed right now takes."""
    return _TIMING.get()


@contextmanager
def timing_override(timing: Timing) -> Generator[None]:
    """Build clients with *timing* instead of :data:`SYSTEM_TIMING`; only clients constructed inside the block."""
    token = _TIMING.set(timing)
    try:
        yield
    finally:
        _TIMING.reset(token)
