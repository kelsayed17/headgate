# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""Per-attempt middleware: caller code that sees, and may change, every request each time it is sent.

:class:`MiddlewareTransport` sits inside the retry layer and above the cache (see :mod:`headgate._stack`), so a
middleware runs once per attempt. Middleware run in the order the client's ``middleware=`` lists them, the first
outermost; each calls ``call_next`` to continue, or returns a response of its own to stop there. A signature that must
be fresh on every attempt belongs in a :class:`~headgate.SigningAuth`, not a middleware: a second
:class:`MiddlewareTransport`, with no middleware, signs each attempt with it below the rate budget, after its wait, and
before the credential guard runs again (see :data:`~headgate._auth.SIGNER_EXT`).

**Credentials a middleware adds stay on trusted origins**, because every middleware declares them. The client's
credential guard (:class:`~headgate.security.CredentialGuard`) runs as a request hook, before the transport stack, so it
cannot see what a middleware adds or what a ``SigningAuth`` signs per attempt; :class:`MiddlewareTransport` runs it
again below the last middleware and the signature, on every attempt of every hop.
A middleware declares the headers and query parameters it stamps (``credential_headers`` and ``credential_params``, the
shape every headgate auth has, empty when it stamps none), and has them removed from a hop addressed to any origin other
than ``base_url``'s and ``SecurityOptions.trusted_origins``. ``Client()`` refuses a middleware that declares neither.

**A host a middleware sends to is vetted too.** Over a network transport that does not pin (a custom one, or a test's
fake network), the SSRF guard's check is a request hook as well, which a middleware's change of host comes after, so it
runs again below the last middleware, after the credential guard, on an attempt the middleware moved to another origin
(the origin it came in with was vetted, and is not looked up twice); the default and curl transports vet the host they
connect to themselves.
"""

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Final, Protocol, override

import httpx2

from headgate._auth import SIGNER_EXT, SigningAuth, refuse_moved, refuse_undeclared, stamped_names
from headgate._core import CredentialCarrier

if TYPE_CHECKING:
    from headgate._core import Clock

__all__ = ["Middleware", "MiddlewareTransport", "Next"]

type Next = Callable[[httpx2.Request], Awaitable[httpx2.Response]]
"""Send a request through the rest of the stack, returning its response."""

#: The request extension holding the origin an attempt came into the middleware with, which the request hooks vetted.
_ENTERED: Final = "headgate.entered_origin"


class Middleware(CredentialCarrier, Protocol):
    """Handle one attempt of a request: change it, then ``await call_next(request)``, or answer it outright.

    A middleware declares the credentials it adds (a signature, a key) with two members, as class attributes or
    properties: ``credential_headers: frozenset[str]``, the names of the headers it sets (compared case-insensitively),
    and ``credential_params: frozenset[str]``, the names of the query parameters it sets. An empty frozenset means it
    stamps none of that kind. The client withholds them from any untrusted origin, a cross-origin redirect hop included
    (see the module docstring). ``Client()`` refuses a middleware that does not declare both, or is not async.
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
    """Run a request through *middleware*, in order, then the signer and *guards*, then the transport below.

    The signer is the client's own :class:`~headgate.SigningAuth`, if it has one. It signs each attempt of a request
    its flow marked (:data:`~headgate._auth.SIGNER_EXT`) as the last middleware passes it on; the stack builds this
    layer twice, once for the middleware and once, below the rate budget, for the signer (see :mod:`headgate._stack`).
    ``aclose`` closes the transport below.
    """

    def __init__(
        self,
        *,
        transport: httpx2.AsyncBaseTransport,
        middleware: tuple[Middleware, ...],
        guards: tuple[Callable[[httpx2.Request], Awaitable[None]], ...],
        signer: tuple[SigningAuth, Clock] | None,
        vets: tuple[Callable[[httpx2.Request], Awaitable[None]], ...] = (),
    ) -> None:
        """Chain *middleware* before *signer*, *guards* and *transport*, once, so each attempt only walks the chain.

        *signer* is the client's auth when it is a ``SigningAuth``, with the client's wall clock, which each signature
        is made at: only its marks are signed, since the guard withholds only what the client's auth declares. *guards*
        run in order on what the last middleware passes on, once it is signed: the client's credential guard. *vets*
        run after them on a request the middleware moved to another origin than it came in with, which the request
        hooks vetted: the SSRF guard's vetting, when the network transport does not pin.
        """
        self._transport = transport

        async def guarded(request: httpx2.Request) -> httpx2.Response:
            if signer is not None and request.extensions.get(SIGNER_EXT) is signer[0]:
                auth, wall_clock = signer
                # A hedge's copy and a redirect hop are new requests over the body the flow buffered: read it again.
                await request.aread()
                before, origin = stamped_names(request), httpx2.Origin(request.url)
                auth.sign(request, wall_clock())
                refuse_undeclared(auth, "sign", request, before)
                refuse_moved(auth, "sign", request, origin)
            for guard in guards:
                await guard(request)
            # A request a middleware built afresh carries no entry origin, so it is vetted too.
            if vets and request.extensions.get(_ENTERED) != httpx2.Origin(request.url):
                for vet in vets:
                    await vet(request)
            return await transport.handle_async_request(request)

        chain: Next = guarded
        for item in reversed(middleware):
            chain = _link(item, chain)
        self._chain = chain

    @override
    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        request.extensions[_ENTERED] = httpx2.Origin(request.url)
        return await self._chain(request)

    @override
    async def aclose(self) -> None:
        """Close the wrapped transport."""
        await self._transport.aclose()
