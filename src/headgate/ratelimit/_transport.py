# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""The rate-limit layer: the bottom of every client's policy stack, spending a budget on each request it sends.

Below the cache, a cache hit returns above it and spends nothing; below retry, every re-send passes through it and
spends its own turn. A client's ``rate_limits`` rules give a matching URL path a budget of its own
(:class:`~headgate.ratelimit.PathBudgets`).

A budget with ``max_in_flight`` counts a request from its send until its response body is closed, not until the
response head arrives: the server is still busy with a request whose body is streaming. So the slot travels with the
response, in a stream wrapper whose ``aclose`` gives it back. Every path that drops a response closes it (the client
after reading the body or on error, the retry layer before re-sending, ``stream()`` on exit), and a send that raises
or is cancelled gives the slot back at once.

**Events.** The budget emits :class:`~headgate._core.RateLimited` for every wait it makes a call take; this layer emits
one more, with the reason ``"pushback"``, for a response that asks the budget to pause, so the call that caused a pause
reports it as well as every call that then waits it out.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, override

import httpx2

from headgate._core import CallContext, RateLimited
from headgate.ratelimit._budget import PUSHBACK

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from headgate.ratelimit._budgets import PathBudgets

__all__ = ["RateLimitTransport"]


class _SlotStream(httpx2.AsyncByteStream):
    """A response body that gives its concurrency slot back when it is closed."""

    def __init__(self, stream: httpx2.AsyncByteStream, release: Callable[[], None]) -> None:
        """Stream *stream*, calling *release* on close."""
        self._stream = stream
        self._release = release

    @override
    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for chunk in self._stream:
            yield chunk

    @override
    async def aclose(self) -> None:
        """Close the wrapped stream, then give the slot back, even if closing raised."""
        try:
            await self._stream.aclose()
        finally:
            self._release()


class RateLimitTransport(httpx2.AsyncBaseTransport):
    """Spend a budget on every request that reaches the network, feed it each response, and close what it wraps.

    ``httpx2.AsyncBaseTransport.aclose`` is a no-op, so the override is what lets a client's teardown reach the cache
    storage and the connection pool below this layer.
    """

    def __init__(self, *, budgets: PathBudgets, transport: httpx2.AsyncBaseTransport) -> None:
        """Pace requests to *transport* on *budgets*."""
        self._budgets = budgets
        self._transport = transport

    @property
    def budgets(self) -> PathBudgets:
        """The budgets this layer chooses from."""
        return self._budgets

    @override
    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        budget = self._budgets.for_request(request)
        context = CallContext.of(request)
        await budget.wait(context)
        release = await budget.occupy()
        # The waits took some of the call's time: the send that follows gets only what is left.
        context.fit_timeouts(request)
        sent_at = budget.now()
        try:
            response = await self._transport.handle_async_request(request)
        except BaseException:
            release()
            raise
        pause = budget.observe(response, sent_at=sent_at)
        if pause is not None and pause > 0 and context.observed:
            context.emit(RateLimited(call=context.info, delay=pause, reason=PUSHBACK))
        if budget.max_in_flight is not None:
            stream = response.stream
            if isinstance(stream, httpx2.AsyncByteStream):
                response.stream = _SlotStream(stream, release)
            else:
                # A synchronous body cannot be closed by an async caller's aclose(); holding the slot would leak it.
                release()
        return response

    @override
    async def aclose(self) -> None:
        """Close the wrapped transport."""
        await self._transport.aclose()
