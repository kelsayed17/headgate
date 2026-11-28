# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""Time and randomness, as one seam: :class:`Timing`.

A :class:`Timing` holds the four things that measure, wait or draw: a monotonic ``clock``, a ``sleep``, a
``wall_clock`` in Unix seconds and a ``random`` source. A client runs on :func:`current_timing` when it is built, and so
does the :class:`~headgate.CachedTokenAuth` it is given; so do a shared :class:`~headgate.RateBudget`,
:class:`~headgate.retry.RetryBudget` and :class:`~headgate.breaker.BreakerRegistry`, none of which takes a timing of its
own. :func:`timing_override` sets it for a block, the one way in, which is how
:meth:`TestClock.install() <headgate.testing.TestClock.install>` reaches everything a test builds.

:class:`Clock`, :class:`Sleep` and :class:`RandomSource` are the protocols each field satisfies; ``time.monotonic``,
``asyncio.sleep``, ``time.time`` and ``random.random`` satisfy them as they are.
"""

from headgate._core._time import Clock, RandomSource, Sleep, Timing, current_timing, timing_override

__all__ = ["Clock", "RandomSource", "Sleep", "Timing", "current_timing", "timing_override"]
