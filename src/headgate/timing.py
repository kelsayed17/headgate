# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""Time and randomness, as one seam: :class:`Timing`.

A :class:`Timing` holds the four things that measure, wait or draw: a monotonic ``clock``, a ``sleep``, a
``wall_clock`` in Unix seconds and a ``random`` source. Everything that takes ``timing=`` (a client, a
:class:`~headgate.CachedTokenAuth`, a shared :class:`~headgate.RateBudget`, a
:class:`~headgate.breaker.BreakerRegistry`) runs on the timing it is given, or, given none, on :func:`current_timing`
when it is built. :func:`timing_override` sets that for a block, which is how
:meth:`TestClock.install() <headgate.testing.TestClock.install>` reaches everything a test builds.

:class:`Clock`, :class:`Sleep` and :class:`RandomSource` are the protocols each field satisfies; ``time.monotonic``,
``asyncio.sleep``, ``time.time`` and ``random.random`` satisfy them as they are.
"""

from headgate._core._time import Clock, RandomSource, Sleep, Timing, current_timing, timing_override

__all__ = ["Clock", "RandomSource", "Sleep", "Timing", "current_timing", "timing_override"]
