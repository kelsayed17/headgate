# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""The running account behind a :class:`~headgate.retry.RetryBudget`: first attempts and retries over a sliding window.

The window is kept as a fixed number of buckets, so memory stays constant however much traffic a client sends: each
bucket counts the first attempts and retries made in its slice of the window, and buckets older than the window are
dropped as time moves on.
"""

from __future__ import annotations

from collections import deque
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from headgate._core import Clock
    from headgate.retry._policy import RetryBudget

__all__ = ["RetryLedger"]

#: How many buckets a window is divided into: the window slides in steps of a tenth of its length.
_BUCKETS: Final = 10


class _Bucket:
    """The first attempts and retries made in one slice of the window."""

    __slots__ = ("firsts", "index", "retries")

    def __init__(self, index: int) -> None:
        """Start an empty bucket for slice number *index*."""
        self.index = index
        self.firsts = 0
        self.retries = 0


class RetryLedger:
    """Count first attempts and retries over a :class:`RetryBudget`'s window, and admit a retry while it fits."""

    __slots__ = ("_buckets", "_budget", "_clock", "_slice")

    def __init__(self, budget: RetryBudget, clock: Clock) -> None:
        """Account for *budget*, measuring the window on its own clock if it has one, else on *clock*."""
        self._budget = budget
        self._clock = budget.clock or clock
        self._slice = budget.window / _BUCKETS
        self._buckets: deque[_Bucket] = deque()

    def _current(self) -> _Bucket:
        """Return the bucket for now, dropping every bucket that has slid out of the window."""
        index = int(self._clock() // self._slice)
        buckets = self._buckets
        while buckets and buckets[0].index <= index - _BUCKETS:
            buckets.popleft()
        if not buckets or buckets[-1].index != index:
            buckets.append(_Bucket(index))
        return buckets[-1]

    def record_first(self) -> None:
        """Count a first attempt, which earns retries their share."""
        self._current().firsts += 1

    def try_spend(self) -> bool:
        """Count a retry and return ``True`` if the window has room for it, else count nothing and return ``False``."""
        current = self._current()
        firsts = sum(bucket.firsts for bucket in self._buckets)
        retries = sum(bucket.retries for bucket in self._buckets)
        allowance = self._budget.ratio * firsts + self._budget.min_per_second * self._budget.window
        if retries + 1 > allowance:
            return False
        current.retries += 1
        return True

    def refund(self) -> None:
        """Take back a retry :meth:`try_spend` counted but never sent, from the latest bucket still counting one."""
        self._current()
        for bucket in reversed(self._buckets):
            if bucket.retries:
                bucket.retries -= 1
                return
