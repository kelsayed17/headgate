# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""A concurrency limit that clients on different event loops, and different threads, can share.

``asyncio.Semaphore`` binds itself to the first loop that waits on it, so one shared by two clients running under two
``asyncio.run`` calls (or two threads) raises on the second. :class:`InFlightLimit` keeps its count under a
``threading.Lock`` and parks each waiter on a future of the waiter's own loop, waking it with
``call_soon_threadsafe``; nothing in it belongs to any one loop.

A slot freed while someone waits is handed straight to the oldest waiter, so the count never dips and a newcomer
cannot overtake. A waiter cancelled after its slot was handed over passes the slot on rather than leaking it.
"""

from __future__ import annotations

import asyncio
import threading
from collections import deque

__all__ = ["InFlightLimit"]


class InFlightLimit:
    """At most ``limit`` holders at once, FIFO, across loops and threads."""

    __slots__ = ("_count", "_limit", "_lock", "_waiters")

    def __init__(self, limit: int) -> None:
        """Admit at most *limit* holders at once."""
        self._limit = limit
        self._count = 0
        self._lock = threading.Lock()
        self._waiters: deque[asyncio.Future[None]] = deque()

    @property
    def limit(self) -> int:
        """How many holders are admitted at once."""
        return self._limit

    @property
    def in_flight(self) -> int:
        """How many slots are held now."""
        with self._lock:
            return self._count

    @property
    def waiting(self) -> int:
        """How many callers are queued for a slot now."""
        with self._lock:
            return len(self._waiters)

    async def acquire(self) -> None:
        """Wait for a slot and take it; :meth:`release` gives it back."""
        with self._lock:
            if self._count < self._limit and not self._waiters:
                self._count += 1
                return
            waiter = asyncio.get_running_loop().create_future()
            self._waiters.append(waiter)
        try:
            await waiter
        except BaseException:
            with self._lock:
                handed_over = waiter not in self._waiters
                if not handed_over:
                    self._waiters.remove(waiter)
            # A slot delivered before the cancellation landed is this caller's to give back; one still on its way finds
            # the waiter cancelled and gives itself back (see _deliver).
            if handed_over and waiter.done() and not waiter.cancelled():
                self.release()
            raise

    def release(self) -> None:
        """Give a slot back: to the oldest waiter whose loop still runs, or to the pool."""
        with self._lock:
            while self._waiters:
                waiter = self._waiters.popleft()
                try:
                    waiter.get_loop().call_soon_threadsafe(self._deliver, waiter)
                except RuntimeError:
                    # Its loop has closed, so it will never resume; the slot goes to the next waiter.
                    continue
                return
            self._count -= 1

    def _deliver(self, waiter: asyncio.Future[None]) -> None:
        """Wake *waiter* with its slot, on its own loop; pass the slot on if it was cancelled meanwhile."""
        if waiter.cancelled():
            self.release()
        else:
            waiter.set_result(None)
