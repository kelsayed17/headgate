# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""The rate-limit layer: the bottom of every client's policy stack, pacing each request that reaches the network.

Below the cache, a cache hit returns above it and spends no token; below retry, every re-send passes through it and
spends its own. A client's ``rate_limits`` rules give a matching URL path a limiter of its own
(:class:`~headgate.ratelimit.PathLimiters`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, override

import httpx2

if TYPE_CHECKING:
    from headgate.ratelimit._limiter import PathLimiters

__all__ = ["RateLimitTransport"]


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
