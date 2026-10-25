# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""A clock for tests: it moves only when a test advances it, or when a sleep through it is awaited."""

from __future__ import annotations

import asyncio


class FakeClock:
    """A monotonic clock a test drives; :meth:`sleep` advances it instead of waiting."""

    def __init__(self, start: float = 0.0) -> None:
        """Start at *start*."""
        self.now = start
        self.slept: list[float] = []

    def __call__(self) -> float:
        """Return the current reading."""
        return self.now

    def advance(self, seconds: float) -> None:
        """Move the clock forward by *seconds*."""
        self.now += seconds

    async def sleep(self, seconds: float, /) -> None:
        """Record *seconds*, advance the clock by them, and yield to the event loop once."""
        self.slept.append(seconds)
        self.advance(seconds)
        await asyncio.sleep(0)
