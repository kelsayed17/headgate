# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""Rate limiting: a client's budget (:class:`Rate`), the limiter that enforces it, and the transport that applies it.

:class:`RateLimitTransport` is the bottom layer of every client's transport stack (see :mod:`headgate._stack`). Below
the cache, a cache hit returns above it and spends no token; below retry, every re-send passes through it and spends
its own. A client's ``rate_limits`` rules give a matching URL path a limiter of its own (:class:`PathLimiters`).

The limiter is chosen by URL path rather than by a request extension: hishel's cache transport rebuilds the request
before handing it down and forwards only its own ``hishel_*`` keys, so a limiter chosen through an extension would be
invisible below the cache. The URL survives.
"""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, override

import httpx2
from aiolimiter import AsyncLimiter

from headgate.policy import policy_is_disabled

if TYPE_CHECKING:
    from collections.abc import Sequence
    from types import TracebackType
    from typing import Self

__all__ = ["UNLIMITED", "AsyncRateLimiter", "PathLimiters", "Rate", "RateLimitTransport", "RateRule"]


@dataclass(frozen=True, slots=True)
class Rate:
    """``requests`` per ``period`` seconds: a client's rate budget, with its unit in the call that builds it.

    ``Rate.per_minute(120)`` and ``Rate.per_second(2)`` are the same budget.
    """

    requests: float
    period: float

    def __post_init__(self) -> None:
        """Reject a budget that cannot pace: non-positive, NaN, or an infinite period."""
        if math.isnan(self.requests) or self.requests <= 0:
            msg = f"requests must be > 0 (got {self.requests})"
            raise ValueError(msg)
        if not math.isfinite(self.period) or self.period <= 0:
            msg = f"period must be > 0 and finite (got {self.period})"
            raise ValueError(msg)

    @classmethod
    def per_second(cls, requests: float) -> Rate:
        """Return a budget of *requests* per second."""
        return cls(requests=requests, period=1.0)

    @classmethod
    def per_minute(cls, requests: float) -> Rate:
        """Return a budget of *requests* per minute."""
        return cls(requests=requests, period=60.0)


#: A rate that never throttles in practice (1M req/s): what a limiter built with transport policy off paces at.
UNLIMITED = Rate.per_second(1_000_000)


@dataclass(frozen=True, slots=True)
class RateRule:
    """A second rate for the requests whose URL path contains ``fragment``.

    The fragment is matched as a substring of the URL path, the same idiom :class:`headgate.PathFragmentFilter` uses
    to scope the cache, and enough for a rule that names one endpoint. The first matching rule wins.
    """

    fragment: str
    rate: Rate


class AsyncRateLimiter:
    """A leaky-bucket limiter (``aiolimiter.AsyncLimiter``) with burst up to ``rate.requests`` and FIFO fairness.

    An async context manager: ``async with limiter:`` takes a slot, which is how :class:`RateLimitTransport` drives
    it. Built with transport policy off (:func:`headgate.policy.policy_disabled`), it paces at :data:`UNLIMITED`;
    enforcing that here rather than at the client also covers the limiters a client's ``rate_limits`` rules build.

    The bucket is created on first use and recreated when the running event loop changes. ``aiolimiter`` binds a
    limiter to the loop that first used it and, on a later loop, recovers only once the first is closed, with a
    ``RuntimeWarning``; a client built once and used under successive ``asyncio.run`` calls must not depend on that.
    The new bucket starts empty, so a loop change admits a fresh burst.
    """

    __slots__ = ("_limiter", "_loop", "_max_rate", "_time_period")

    def __init__(self, rate: Rate) -> None:
        """Build a limiter pacing at *rate*, or at :data:`UNLIMITED` when transport policy is off."""
        if policy_is_disabled():
            rate = UNLIMITED
        max_rate, time_period = rate.requests, rate.period
        # aiolimiter wants max_rate >= 1, so a sub-1 rate scales the period instead: 0.5 per 1s becomes 1 per 2s.
        if max_rate < 1:
            time_period = time_period / max_rate
            max_rate = 1.0
        self._max_rate = max_rate
        self._time_period = time_period
        self._limiter: AsyncLimiter | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    @property
    def rate(self) -> Rate:
        """The budget the bucket enforces: the constructor's, or :data:`UNLIMITED`, with a sub-1 rate rescaled."""
        return Rate(requests=self._max_rate, period=self._time_period)

    def _get_limiter(self) -> AsyncLimiter:
        """Return the running loop's bucket, building a new one if the loop has changed."""
        loop = asyncio.get_running_loop()
        if self._limiter is None or loop is not self._loop:
            self._limiter = AsyncLimiter(self._max_rate, self._time_period)
            self._loop = loop
        return self._limiter

    async def wait(self) -> None:
        """Block until a request slot is available (FIFO, async-safe)."""
        await self._get_limiter().acquire()

    async def __aenter__(self) -> Self:
        """Take a slot through :meth:`wait`, so patching ``wait`` un-throttles every request in a test suite."""
        await self.wait()
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc_value: BaseException | None, traceback: TracebackType | None
    ) -> None:
        """Release nothing: a leaky bucket's slot is spent on entry."""


class PathLimiters:
    """A client's limiters, chosen by URL path (see the module docstring for why not by extension).

    Every limiter is built at construction. The first rule whose fragment occurs in the URL path wins; any other
    request paces on :attr:`default`.
    """

    __slots__ = ("_rules", "default")

    def __init__(self, *, default: Rate, rules: Sequence[RateRule] = ()) -> None:
        """Build the default limiter and one limiter per rule, in rule order."""
        self.default = AsyncRateLimiter(default)
        self._rules = tuple((rule.fragment, AsyncRateLimiter(rule.rate)) for rule in rules)

    def for_request(self, request: httpx2.Request) -> AsyncRateLimiter:
        """Return the limiter *request* paces on: the first matching rule's, else :attr:`default`."""
        path = request.url.path
        return next((limiter for fragment, limiter in self._rules if fragment in path), self.default)


class RateLimitTransport(httpx2.AsyncBaseTransport):
    """Pace every request that reaches the network, and close the transport it wraps.

    ``httpx2.AsyncBaseTransport.aclose`` is a no-op, so the override is what lets a client's teardown reach the cache
    storage and the connection pool below this layer.
    """

    def __init__(self, *, limiters: PathLimiters, transport: httpx2.AsyncBaseTransport) -> None:
        """Pace requests to *transport* on *limiters*."""
        self._limiters = limiters
        self._transport = transport

    @property
    def limiters(self) -> PathLimiters:
        """The limiters this layer chooses from."""
        return self._limiters

    @override
    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        async with self._limiters.for_request(request):
            return await self._transport.handle_async_request(request)

    @override
    async def aclose(self) -> None:
        """Close the wrapped transport."""
        await self._transport.aclose()
