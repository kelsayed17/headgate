# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""Per-attempt middleware: caller code that sees, and may change, every request each time it is sent.

:class:`MiddlewareTransport` sits inside the retry layer and above the cache (see :mod:`headgate._stack`), so a
middleware runs once per attempt. Middleware run in the order ``HttpOptions(middleware=...)`` lists them, the first
outermost; each calls ``call_next`` to continue, or returns a response of its own to stop there. A signature that must
be fresh on every attempt belongs in a :class:`~headgate.SigningAuth`, not a middleware: the same transport signs each
attempt with it, after the last middleware and before the credential guard (see :data:`~headgate._auth.SIGNER_EXT`).

**Credentials a middleware adds stay on trusted origins**, because every middleware declares them. The client's
credential guard (:class:`~headgate.security.CredentialGuard`) runs as a request hook, before the transport stack, so it
cannot see what a middleware adds or what a ``SigningAuth`` signs per attempt; :class:`MiddlewareTransport` runs it
again below the last middleware and the signature, on every attempt of every hop.
A middleware declares the headers and query parameters it stamps (``credential_headers`` and ``credential_params``, the
shape every headgate auth has, empty when it stamps none), and has them removed from a hop addressed to any origin other
than ``base_url``'s and ``SecurityOptions.trusted_origins``. ``HttpOptions`` refuses a middleware that declares neither.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, override

import httpx2

from headgate._auth import SIGNER_EXT, SigningAuth
from headgate._core import CredentialCarrier

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

__all__ = ["Middleware", "MiddlewareTransport", "Next"]

type Next = Callable[[httpx2.Request], Awaitable[httpx2.Response]]
"""Send a request through the rest of the stack, returning its response."""


class Middleware(CredentialCarrier, Protocol):
    """Handle one attempt of a request: change it, then ``await call_next(request)``, or answer it outright.

    A middleware declares the credentials it adds (a signature, a key) with two members, as class attributes or
    properties: ``credential_headers: frozenset[str]``, the names of the headers it sets (compared case-insensitively),
    and ``credential_params: frozenset[str]``, the names of the query parameters it sets. An empty frozenset means it
    stamps none of that kind. The client withholds them from any untrusted origin, a cross-origin redirect hop included
    (see the module docstring). ``HttpOptions`` refuses a middleware that does not declare both.
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
    """Run a request through *middleware*, in order, then its signer and *guard*, then the transport below.

    The signer is the :class:`~headgate.SigningAuth` a request's auth flow set in :data:`~headgate._auth.SIGNER_EXT`,
    if any; it signs each attempt of that request as the last middleware passes it on. ``aclose`` closes the transport
    below.
    """

    def __init__(
        self,
        *,
        transport: httpx2.AsyncBaseTransport,
        middleware: tuple[Middleware, ...],
        guard: Callable[[httpx2.Request], Awaitable[None]],
    ) -> None:
        """Chain *middleware* before the signer, *guard* and *transport*, once, so each attempt only walks the chain.

        *guard* is the client's credential guard, run on what the last middleware passes on, once it is signed.
        """
        self._transport = transport

        async def guarded(request: httpx2.Request) -> httpx2.Response:
            signer = request.extensions.get(SIGNER_EXT)
            if isinstance(signer, SigningAuth):
                # A hedge's copy and a redirect hop are new requests over the body the flow buffered: read it again.
                await request.aread()
                signer.sign(request)
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
