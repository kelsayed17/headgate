# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""The running account inside a :class:`~headgate.retry.RetryBudget`: first attempts and retries over a sliding window.

The window is kept as a fixed number of buckets, so memory stays constant however much traffic a client sends: each
bucket counts the first attempts and retries made in its slice of the window, and buckets older than the window are
dropped as time moves on.
"""

from __future__ import annotations

from collections import deque
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from headgate._core import Clock
    from headgate.retry._policy import RetryShare

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
    """Count first attempts and retries over a :class:`RetryShare`'s window, and admit a retry while it fits."""

    __slots__ = ("_buckets", "_clock", "_share", "_slice")

    def __init__(self, share: RetryShare, clock: Clock) -> None:
        """Account for *share*, measuring the window on *clock*."""
        self._share = share
        self._clock = clock
        self._slice = share.window / _BUCKETS
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
        allowance = self._share.ratio * firsts + self._share.min_per_second * self._share.window
        if retries + 1 > allowance:
            return False
        current.retries += 1
        return True

    def counts(self) -> tuple[int, int]:
        """Return the retries and the first attempts the window holds now."""
        self._current()
        return sum(bucket.retries for bucket in self._buckets), sum(bucket.firsts for bucket in self._buckets)

    def refund(self) -> None:
        """Take back a retry :meth:`try_spend` counted but never sent, from the latest bucket still counting one."""
        self._current()
        for bucket in reversed(self._buckets):
            if bucket.retries:
                bucket.retries -= 1
                return
