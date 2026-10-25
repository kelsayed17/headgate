# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""Per-attempt middleware: caller code that sees, and may change, every request each time it is sent.

:class:`MiddlewareTransport` sits inside the retry layer and above the cache (see :mod:`headgate._stack`), so a
middleware runs once per attempt: a request signer stamps a fresh timestamp and nonce on every re-send, where an
``httpx2.Auth`` flow, which runs once per call above the retry layer, would repeat the first signature. Middleware run
in the order ``HttpOptions(middleware=...)`` lists them, the first outermost; each calls ``call_next`` to continue, or
returns a response of its own to stop there.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, override

import httpx2

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

__all__ = ["Middleware", "MiddlewareTransport", "Next"]

type Next = Callable[[httpx2.Request], Awaitable[httpx2.Response]]
"""Send a request through the rest of the stack, returning its response."""


class Middleware(Protocol):
    """Handle one attempt of a request: change it, then ``await call_next(request)``, or answer it outright."""

    async def __call__(self, request: httpx2.Request, call_next: Next) -> httpx2.Response:
        """Return the response for *request*, usually ``await call_next(request)``."""
        ...


def _link(middleware: Middleware, call_next: Next) -> Next:
    """Return a step that runs *middleware* with *call_next* as the rest of the chain."""

    async def step(request: httpx2.Request) -> httpx2.Response:
        return await middleware(request, call_next)

    return step


class MiddlewareTransport(httpx2.AsyncBaseTransport):
    """Run a request through *middleware*, in order, then the transport below; close that transport on ``aclose``."""

    def __init__(self, *, transport: httpx2.AsyncBaseTransport, middleware: tuple[Middleware, ...]) -> None:
        """Chain *middleware* in front of *transport*, once, so each attempt only walks the chain."""
        self._transport = transport
        chain: Next = transport.handle_async_request
        for item in reversed(middleware):
            chain = _link(item, chain)
        self._chain = chain

    @override
    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        return await self._chain(request)

    @override
    async def aclose(self) -> None:
        """Close the wrapped transport."""
        await self._transport.aclose()
