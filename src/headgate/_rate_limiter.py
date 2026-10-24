"""Async rate limiter for outbound HTTP clients.

Wraps ``aiolimiter.AsyncLimiter`` (leaky-bucket algorithm) with a convenience API for the data client layer.  Supports:

* Burst allowance (up to a :class:`Rate`'s ``requests`` instant requests before throttling)
* Fair FIFO ordering under contention

Usage::

    from headgate._rate_limiter import AsyncRateLimiter, Rate

    limiter = AsyncRateLimiter(Rate.per_second(5))   # 5 requests per second
    await limiter.wait()                             # blocks until a slot opens

    limiter = AsyncRateLimiter(Rate.per_minute(10))  # 10 requests per minute
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING

from aiolimiter import AsyncLimiter

from headgate.policy import policy_is_disabled

if TYPE_CHECKING:
    from types import TracebackType
    from typing import Self

#: Requests per second that never throttles in practice. It builds ``UNLIMITED`` below, which the policy-off
#: path in ``AsyncRateLimiter.__init__`` assigns.
_UNLIMITED_RATE = 1_000_000.0


@dataclass(frozen=True, slots=True)
class Rate:
    """``requests`` per ``period`` seconds: a client's rate budget, with its unit in the call that builds it.

    ``Rate.per_minute(120)`` and ``Rate.per_second(2)`` are the same budget. Previously a
    client passed ``max_rate=`` with an optional ``rate_period=``, so the literal 20 meant
    1200/min on one client and 20/min on the next depending on a second argument, and an
    ast-grep rule policed constant NAMES to keep the two in step. The unit is in the type now.
    """

    requests: float
    period: float

    def __post_init__(self) -> None:
        if self.requests <= 0:
            raise ValueError(f"requests must be > 0 (got {self.requests})")
        if self.period <= 0:
            raise ValueError(f"period must be > 0 (got {self.period})")

    @classmethod
    def per_second(cls, requests: float) -> Rate:
        return cls(requests=requests, period=1.0)

    @classmethod
    def per_minute(cls, requests: float) -> Rate:
        return cls(requests=requests, period=60.0)


#: A rate that never throttles in practice (1M req/s). Used by the policy-off path below and by the
#: client tests that need the throttle out of the way (``rate=UNLIMITED``).
UNLIMITED = Rate.per_second(_UNLIMITED_RATE)


class AsyncRateLimiter:
    """Rate limiter backed by ``aiolimiter.AsyncLimiter`` (leaky bucket).

    An async context manager — ``async with limiter:`` takes a slot — which is how
    :class:`headgate._transports.RateLimitTransport` drives it from inside the
    transport stack while the rate POLICY (burst, sub-1/s scaling, the policy-off switch,
    recreation on an event-loop change) stays here.

    Parameters
    ----------
    rate:
        The budget (see :class:`Rate`).
    """

    # Effective Now when the class stopped subclassing httpx-limiter's
    # ``AbstractAsyncLimiter`` (which declared no ``__slots__`` of its own, so instances used
    # to carry a ``__dict__`` regardless and this was only the field inventory). A subclass
    # that adds an attribute of its own still gets a ``__dict__`` for it.
    __slots__ = ("_limiter", "_loop", "_max_rate", "_time_period")

    def __init__(self, rate: Rate) -> None:
        # Transport policy off (headgate.policy.policy_disabled). Enforced HERE rather than
        # at the client, because a client may declare SECONDARY limiters -- example's candle
        # ``rate_limits`` rule, which ``PathLimiters`` turns into an ``AsyncRateLimiter`` built from its
        # ``Rate`` -- and every limiter constructed inside the block is covered.
        if policy_is_disabled():
            rate = UNLIMITED
        max_rate, time_period = rate.requests, rate.period
        # Sub-1 rates are valid; aiolimiter wants max_rate >= 1, so ``time_period`` is scaled to preserve
        # the requested rate. E.g. 0.5 per 1s becomes 1 per 2s — same rate, no silent clamping.
        if max_rate < 1:
            time_period = time_period / max_rate
            max_rate = 1.0
        self._max_rate = max_rate
        self._time_period = time_period
        self._limiter: AsyncLimiter | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def _get_limiter(self) -> AsyncLimiter:
        """Return the limiter, recreating it if the running loop has changed."""
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None
        if self._limiter is None or current_loop is not self._loop:
            self._limiter = AsyncLimiter(self._max_rate, self._time_period)
            self._loop = current_loop
        return self._limiter

    async def wait(self) -> None:
        """Block until a request slot is available (FIFO, async-safe)."""
        await self._get_limiter().acquire()

    async def __aenter__(self) -> Self:
        """Take a slot — the interface :class:`~headgate._transports.RateLimitTransport` acquires through.

        Delegates to :meth:`wait` rather than to the bucket directly, so that the ONE
        seam a test monkeypatches to un-throttle a suite (``AsyncRateLimiter.wait``)
        still covers requests paced from inside the transport stack. ``__aexit__`` releases
        nothing: a leaky bucket has nothing to release.
        """
        await self.wait()
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc_value: BaseException | None, traceback: TracebackType | None
    ) -> None:
        """Nothing to release: a leaky bucket's slot is spent on entry."""
