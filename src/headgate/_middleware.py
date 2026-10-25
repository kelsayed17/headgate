# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""Per-attempt middleware: caller code that sees, and may change, every request each time it is sent.

:class:`MiddlewareTransport` sits inside the retry layer and above the cache (see :mod:`headgate._stack`), so a
middleware runs once per attempt: a request signer stamps a fresh timestamp and nonce on every re-send, where an
``httpx2.Auth`` flow, which runs once per call above the retry layer, would repeat the first signature. Middleware run
in the order ``HttpOptions(middleware=...)`` lists them, the first outermost; each calls ``call_next`` to continue, or
returns a response of its own to stop there.

**Credentials a middleware adds stay on trusted origins** when it declares them. The client's credential guard
(:class:`~headgate.security.CredentialGuard`) runs as a request hook, before the transport stack, so it cannot see what
a middleware adds; :class:`MiddlewareTransport` runs it again below the last middleware, on every attempt of every hop.
A middleware that declares the headers and query parameters it stamps (``credential_headers`` and
``credential_params``, the shape every headgate auth has) has them removed from a hop addressed to any origin other than
``base_url``'s and ``HttpOptions.trusted_origins``. One that declares nothing is trusted to check the destination
itself.
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
    """Handle one attempt of a request: change it, then ``await call_next(request)``, or answer it outright.

    A middleware that adds a credential (a signature, a key) declares its names as ``credential_headers`` and
    ``credential_params`` (frozensets, either empty), so the client withholds them from any untrusted origin, a
    cross-origin redirect hop included (see the module docstring). Without the declaration they ride every hop.
    """

    async def __call__(self, request: httpx2.Request, call_next: Next) -> httpx2.Response:
        """Return the response for *request*, usually ``await call_next(request)``."""
        ...


def _link(middleware: Middleware, call_next: Next) -> Next:
    """Return a step that runs *middleware* with *call_next* as the rest of the chain."""

    async def step(request: httpx2.Request) -> httpx2.Response:
        return await middleware(request, call_next)

    return step


class MiddlewareTransport(httpx2.AsyncBaseTransport):
    """Run a request through *middleware*, in order, then *guard*, then the transport below; close it on ``aclose``."""

    def __init__(
        self,
        *,
        transport: httpx2.AsyncBaseTransport,
        middleware: tuple[Middleware, ...],
        guard: Callable[[httpx2.Request], Awaitable[None]],
    ) -> None:
        """Chain *middleware* in front of *guard* and *transport*, once, so each attempt only walks the chain.

        *guard* is the client's credential guard, run on what the last middleware passes on.
        """
        self._transport = transport

        async def guarded(request: httpx2.Request) -> httpx2.Response:
            await guard(request)
            return await transport.handle_async_request(request)

        chain: Next = guarded
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
