# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""The SSRF guard in the two places it runs: the default network transport, and a request hook for any other.

**Pinning happens at connect time.** :class:`PinnedHTTPTransport` gives httpx2's connection pool a network backend
that resolves the host, vets every address, and connects to a vetted address in the same step, so the address checked
is the address connected and a rebinding DNS answer has no window to switch targets. A pooled connection stays pinned
to the address it was opened to for as long as it lives. The request keeps its host name, so the pool keys connections
by name, TLS sends that name as SNI and verifies the certificate against it, and HTTP/2's ``:authority`` carries it,
all as without the guard.

Rewriting the request URL to the resolved IP (with the name moved to ``Host`` and the ``sni_hostname`` extension) would
pin too, but the pool would then key connections by IP: a request to one name could ride a TLS connection
authenticated for another name on the same address, which is exactly the substitution a DNS attacker wants. It would
also put the IP into ``response.url``, relative redirects and the credential origin rule.

**A custom transport** (``HttpOptions(transport=...)``, the curl transport included) resolves names itself, so the
guard cannot choose its address: :func:`address_hook` vets every hop as a request hook, which refuses a forbidden host
before anything is sent, but the transport's own lookup could still be answered differently a moment later. Only the
default transport pins.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING, override

import httpcore2
import httpx2
from httpcore2._backends.anyio import AnyIOBackend

if TYPE_CHECKING:
    import ssl
    from collections.abc import Awaitable, Callable, Iterable

    from httpcore2._backends.base import SOCKET_OPTION

    from headgate.security._addresses import AddressGuard

__all__ = ["PinnedHTTPTransport", "address_hook"]


class _VettingBackend(httpcore2.AsyncNetworkBackend):
    """A network backend that connects only to addresses its guard vetted, resolving and vetting on every connect."""

    def __init__(self, guard: AddressGuard) -> None:
        """Vet through *guard*, and connect through httpcore2's asyncio backend."""
        self._guard = guard
        self._backend = AnyIOBackend()

    @override
    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[SOCKET_OPTION] | None = None,
    ) -> httpcore2.AsyncNetworkStream:
        # The lookup counts against the connect timeout, as it does for an unguarded connection.
        try:
            async with asyncio.timeout(timeout):
                addresses = await self._guard.vet(host, port)
        except TimeoutError as exc:
            raise httpcore2.ConnectTimeout(str(exc)) from exc
        except OSError as exc:
            raise httpcore2.ConnectError(str(exc)) from exc
        # Each vetted address in turn, as an unguarded connect falls back along the resolver's answer.
        *earlier, last = addresses
        for address in earlier:
            with contextlib.suppress(httpcore2.ConnectError):
                return await self._backend.connect_tcp(address, port, timeout, local_address, socket_options)
        return await self._backend.connect_tcp(last, port, timeout, local_address, socket_options)


class PinnedHTTPTransport(httpx2.AsyncHTTPTransport):
    """httpx2's HTTP/2-capable transport, connecting only to addresses an :class:`AddressGuard` vetted.

    The transport ``AsyncBaseClient`` builds when ``HttpOptions(allow_private_hosts=False)`` and no custom transport is
    given. A refused address raises :class:`~headgate.errors.ForbiddenDestinationError` from the send; a name that does
    not resolve raises ``httpx2.ConnectError``, as it would unguarded. It reads no environment and cannot use a proxy,
    since a proxy resolves the target itself.
    """

    def __init__(self, guard: AddressGuard, *, ssl_context: ssl.SSLContext | None, limits: httpx2.Limits) -> None:
        """Connect through *guard*, verifying TLS with *ssl_context* (the default trust store when ``None``)."""
        context = httpx2.create_ssl_context(trust_env=False) if ssl_context is None else ssl_context
        super().__init__(verify=context, http2=True, limits=limits, trust_env=False)
        # httpx2 does not take a network backend, so the pool it built is replaced by an identical one that has one.
        self._pool = httpcore2.AsyncConnectionPool(
            ssl_context=context,
            max_connections=limits.max_connections,
            max_keepalive_connections=limits.max_keepalive_connections,
            keepalive_expiry=limits.keepalive_expiry,
            http1=True,
            http2=True,
            network_backend=_VettingBackend(guard),
        )


def address_hook(guard: AddressGuard) -> Callable[[httpx2.Request], Awaitable[None]]:
    """Build a request hook vetting every hop's host through *guard*, for a transport that resolves names itself.

    A refused host raises :class:`~headgate.errors.ForbiddenDestinationError`; a name that does not resolve raises
    ``httpx2.ConnectError``, the failure the transport would have raised.
    """

    async def vet(request: httpx2.Request) -> None:
        url = request.url
        host = url.raw_host.decode("ascii")
        try:
            await guard.vet(host, url.origin.port or 0)
        except OSError as exc:
            msg = f"could not resolve {host}: {exc}"
            raise httpx2.ConnectError(msg, request=request) from exc

    return vet
