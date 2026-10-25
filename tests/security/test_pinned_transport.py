# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""``PinnedHTTPTransport`` against real local servers: the address vetted is the address connected.

The guard's real check refuses loopback, so these tests widen it with ``AddressGuard(check=...)`` to admit exactly the
loopback addresses a test stands in for "public", and resolve names with a scripted resolver. Everything else is the
production path: httpx2's pool, TLS with SNI, HTTP/2.
"""

from __future__ import annotations

import asyncio
import socket
from typing import TYPE_CHECKING, Final

import httpx2
import pytest

from headgate import ForbiddenDestinationError, HttpOptions, Timeouts, TransportError
from headgate._options import DEFAULT_LIMITS, TransportPolicy
from headgate.errors import ClientTimeoutError
from headgate.ratelimit import Rate
from headgate.retry import NO_RETRY
from headgate.security import AddressGuard, PinnedHTTPTransport
from tests.security._servers import client_ssl_context, serving
from tests.support.clients import StubClient
from tests.support.responses import RESPONSE

if TYPE_CHECKING:
    from collections.abc import Sequence

#: The address a test's server listens on, standing in for a public address.
_PUBLIC: Final = "127.0.0.1"
#: A loopback address standing in for a private one.
_PRIVATE: Final = "127.0.0.2"
#: An address no test server listens on (the servers bind IPv4 only), which refuses a connection at once.
_UNLISTENED: Final = "::1"


def _only_public(address: str) -> str | None:
    """Admit only :data:`_PUBLIC`."""
    return None if address == _PUBLIC else "test-private"


class _Resolver:
    """Answer every lookup with the current *answer*, counting lookups."""

    def __init__(self, *answer: str) -> None:
        """Answer with *answer* until a test changes it."""
        self.answer: Sequence[str] = answer
        self.lookups: list[str] = []

    async def __call__(self, host: str, _port: int) -> Sequence[str]:
        """Record *host* and return the current answer."""
        self.lookups.append(host)
        return self.answer


def _client(base_url: str, guard: AddressGuard, *, tls: bool = False, timeout: Timeouts | None = None) -> StubClient:
    """Build a client over a pinned transport through *guard*, sending each call once."""
    transport = PinnedHTTPTransport(guard, ssl_context=client_ssl_context() if tls else None, limits=DEFAULT_LIMITS)
    return StubClient(
        base_url=base_url,
        rate=Rate.per_second(1000),
        policy=TransportPolicy(retry=NO_RETRY),
        http=HttpOptions(transport=transport, timeout=timeout or Timeouts()),
    )


async def test_http2_over_tls_sends_the_name_as_sni_and_authority() -> None:
    """Connect to the vetted address while TLS and HTTP/2 still carry the host name, and the certificate verifies."""
    resolver = _Resolver(_PUBLIC)
    async with (
        serving(tls=True) as server,
        _client(f"https://api.example:{server.port}", AddressGuard(resolver, check=_only_public), tls=True) as client,
    ):
        resp = await client.get("ping", expect=RESPONSE)

    assert resp.http_version == "HTTP/2"
    assert server.server_names == ["api.example"]
    assert server.requests[0].headers[":authority"] == f"api.example:{server.port}"
    assert resolver.lookups == ["api.example"]


async def test_a_pooled_connection_stays_pinned_when_the_name_rebinds() -> None:
    """Keep using the vetted connection after the name rebinds, and refuse the rebound address on a new connection."""
    resolver = _Resolver(_PUBLIC)
    guard = AddressGuard(resolver, check=_only_public)
    async with serving() as server:
        async with _client(f"http://api.example:{server.port}", guard) as client:
            await client.get("first", expect=RESPONSE)
            resolver.answer = (_PRIVATE,)
            await client.get("second", expect=RESPONSE)
        async with _client(f"http://api.example:{server.port}", guard) as fresh:
            with pytest.raises(ForbiddenDestinationError, match=r"127\.0\.0\.2"):
                await fresh.get("third", expect=RESPONSE)

    assert [seen.path for seen in server.requests] == ["/first", "/second"]
    assert server.connections == 1
    assert resolver.lookups == ["api.example", "api.example"]


async def test_one_forbidden_address_refuses_the_name() -> None:
    """Refuse a name when any address it resolves to is forbidden, connecting to none of them."""
    async with serving() as server:
        guard = AddressGuard(_Resolver(_PUBLIC, _PRIVATE), check=_only_public)
        async with _client(f"http://api.example:{server.port}", guard) as client:
            with pytest.raises(ForbiddenDestinationError):
                await client.get("x", expect=RESPONSE)

    assert server.connections == 0


async def test_an_unreachable_address_falls_back_to_the_next_vetted_one() -> None:
    """Try the vetted addresses in order, as an unguarded connect does."""
    async with serving() as server:
        guard = AddressGuard(_Resolver(_UNLISTENED, _PUBLIC), check=lambda _address: None)
        async with _client(f"http://api.example:{server.port}", guard) as client:
            await client.get("x", expect=RESPONSE)

    assert server.connections == 1


async def test_a_name_that_does_not_resolve_is_a_connect_error() -> None:
    """Raise a lookup failure as the ``TransportError`` an unguarded connect raises, naming the host."""

    async def fail(_host: str, _port: int) -> Sequence[str]:
        msg = "nodename nor servname provided"
        raise socket.gaierror(msg)

    async with _client("http://api.example", AddressGuard(fail)) as client:
        with pytest.raises(TransportError, match="nodename") as exc:
            await client.get("x", expect=RESPONSE)

    assert exc.value.retryable


async def test_a_lookup_counts_against_the_connect_timeout() -> None:
    """Bound a lookup that hangs by the connect timeout, raising ``ClientTimeoutError``."""

    async def hang(_host: str, _port: int) -> Sequence[str]:
        await asyncio.Event().wait()
        return ()

    async with _client("http://api.example", AddressGuard(hang), timeout=Timeouts(connect=0.01)) as client:
        with pytest.raises(ClientTimeoutError):
            await client.get("x", expect=RESPONSE)


async def test_the_default_trust_store_is_used_without_an_ssl_context() -> None:
    """Verify TLS against the default trust store when no context is given, so the test CA is not trusted."""
    async with serving(tls=True) as server:
        guard = AddressGuard(_Resolver(_PUBLIC), check=_only_public)
        async with _client(f"https://api.example:{server.port}", guard) as client:
            with pytest.raises(TransportError, match="certificate"):
                await client.get("x", expect=RESPONSE)


def test_the_transport_takes_httpx2_s_pool_options() -> None:
    """Accept the same pool limits the default transport does."""
    transport = PinnedHTTPTransport(AddressGuard(), ssl_context=None, limits=httpx2.Limits(max_connections=1))

    assert isinstance(transport, httpx2.AsyncHTTPTransport)
