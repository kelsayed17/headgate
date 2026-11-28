# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""``AsyncCurlTransport`` under the SSRF guard: curl connects only to the addresses the guard vetted.

The guard's real check refuses loopback, so the real-curl tests widen it with a check (``check=``) to admit
exactly the loopback addresses a test stands in for "public", and resolve names with a scripted resolver. The names
(``api.example``) do not resolve in real DNS, so a request that reaches the server proves curl connected to the pinned
address rather than looking the name up itself.
"""

from __future__ import annotations

import asyncio
import copy
import socket
import types
from typing import TYPE_CHECKING, Any, Final, cast

import httpx2
import pytest
from curl_cffi import CurlOpt

from headgate import Client, HttpOptions, Rate, SecurityOptions
from headgate._curl import guard_binding
from headgate._options import CallPolicy
from headgate.curl import AsyncCurlTransport
from headgate.errors import ForbiddenDestinationError
from headgate.security._addresses import AddressGuard
from tests.security._servers import CERTS, serving
from tests.support.responses import RESPONSE

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Sequence

#: The address a test's server listens on, standing in for a public address.
_PUBLIC: Final = "127.0.0.1"
#: A loopback address standing in for a private one.
_PRIVATE: Final = "127.0.0.2"
#: IPv6 loopback: a server listens there only when a test binds it, so it otherwise refuses a connection at once.
_V6: Final = "::1"


def _binds_v6() -> bool:
    """Say whether this host can listen on IPv6 loopback: some containers and CI runners have no IPv6."""
    try:
        with socket.socket(socket.AF_INET6) as probe:
            probe.bind((_V6, 0))
    except OSError:
        return False
    return True


#: Skip a test that listens on IPv6 loopback where the host cannot.
_needs_v6: Final = pytest.mark.skipif(not _binds_v6(), reason="this host cannot listen on IPv6 loopback")


def _admitting(*allowed: str) -> Callable[[str], str | None]:
    """Return a check admitting only *allowed*."""
    return lambda address: None if address in allowed else "test-private"


class _Resolver:
    """Answer every lookup with the current *answer*, recording each lookup."""

    def __init__(self, *answer: str) -> None:
        """Answer with *answer* until a test changes it."""
        self.answer: Sequence[str] = answer
        self.lookups: list[str] = []

    async def __call__(self, host: str, _port: int) -> Sequence[str]:
        """Record *host* and return the current answer."""
        self.lookups.append(host)
        return self.answer


def _pinned(guard: AddressGuard, curl_options: dict[CurlOpt, Any] | None = None) -> AsyncCurlTransport:
    """Return a curl transport bound to *guard*, as a guarded client's construction binds it."""
    transport = AsyncCurlTransport(impersonate="chrome", curl_options=curl_options)
    binding = guard_binding(transport)
    assert binding is not None, "a curl transport pins its requests itself"
    binding.bind(guard)
    return transport


class TestRealCurl:
    """Drive real curl, pinned, against local servers."""

    async def test_curl_connects_to_the_vetted_address_not_its_own_lookup(self) -> None:
        """Reach the server under a name only the guard's resolver knows, looked up once."""
        resolver = _Resolver(_PUBLIC)
        guard = AddressGuard(resolver, check=_admitting(_PUBLIC))
        async with serving() as server, httpx2.AsyncClient(transport=_pinned(guard)) as client:
            response = await client.get(f"http://api.example:{server.port}/x")

        assert response.status_code == httpx2.codes.OK
        assert resolver.lookups == ["api.example"]

    async def test_a_name_that_rebinds_after_the_check_is_never_reached(self) -> None:
        """Vet every request, so a name rebound to a private address is refused even over a pooled connection."""
        resolver = _Resolver(_PUBLIC)
        guard = AddressGuard(resolver, check=_admitting(_PUBLIC))
        async with serving() as server, httpx2.AsyncClient(transport=_pinned(guard)) as client:
            await client.get(f"http://api.example:{server.port}/first")
            resolver.answer = (_PRIVATE,)
            with pytest.raises(ForbiddenDestinationError, match=r"127\.0\.0\.2"):
                await client.get(f"http://api.example:{server.port}/second")

        assert [seen.path for seen in server.requests] == ["/first"]

    @_needs_v6
    async def test_an_ipv6_address_is_pinned_in_brackets(self) -> None:
        """Pin a name to an IPv6 address, which curl reads only in brackets."""
        resolver = _Resolver(_V6)
        guard = AddressGuard(resolver, check=_admitting(_V6))
        async with serving(host=_V6) as server, httpx2.AsyncClient(transport=_pinned(guard)) as client:
            response = await client.get(f"http://api.example:{server.port}/x")

        assert response.status_code == httpx2.codes.OK
        assert resolver.lookups == ["api.example"]

    @pytest.mark.parametrize(
        ("bound", "host"), [(_PUBLIC, _PUBLIC), pytest.param(_V6, f"[{_V6}]", marks=_needs_v6)], ids=["ipv4", "ipv6"]
    )
    async def test_an_ip_literal_host_is_reached_without_a_lookup(self, bound: str, host: str) -> None:
        """Connect to a literal address as it is, checked but never looked up."""
        resolver = _Resolver()
        guard = AddressGuard(resolver, check=_admitting(bound))
        async with serving(host=bound) as server, httpx2.AsyncClient(transport=_pinned(guard)) as client:
            response = await client.get(f"http://{host}:{server.port}/x")

        assert response.status_code == httpx2.codes.OK
        assert resolver.lookups == []

    async def test_a_forbidden_ip_literal_is_never_connected(self) -> None:
        """Refuse a literal address the check forbids before curl connects."""
        guard = AddressGuard(_Resolver(), check=_admitting(_PUBLIC))
        async with serving() as server, httpx2.AsyncClient(transport=_pinned(guard)) as client:
            with pytest.raises(ForbiddenDestinationError):
                await client.get(f"http://{_PRIVATE}:{server.port}/x")

        assert server.connections == 0

    async def test_a_name_with_several_addresses_falls_back_along_them(self) -> None:
        """Pin every vetted address, so curl falls back from one that refuses to the next, as unguarded."""
        guard = AddressGuard(_Resolver(_V6, _PUBLIC), check=_admitting(_V6, _PUBLIC))
        async with serving() as server, httpx2.AsyncClient(transport=_pinned(guard)) as client:
            response = await client.get(f"http://api.example:{server.port}/x")

        assert response.status_code == httpx2.codes.OK
        assert server.connections == 1

    async def test_tls_still_sends_and_verifies_the_name(self) -> None:
        """Keep the URL's name for SNI and the certificate check while connecting to the pinned address."""
        guard = AddressGuard(_Resolver(_PUBLIC), check=_admitting(_PUBLIC))
        transport = _pinned(guard, {CurlOpt.CAINFO: str(CERTS / "ca.pem")})
        async with serving(tls=True) as server, httpx2.AsyncClient(transport=transport) as client:
            response = await client.get(f"https://api.example:{server.port}/x")

        assert response.status_code == httpx2.codes.OK
        assert server.server_names == ["api.example"]

    async def test_concurrent_requests_to_one_name_each_reach_a_vetted_address(self) -> None:
        """Pin concurrent requests to one host and port, each through its own vetting."""
        resolver = _Resolver(_PUBLIC)
        guard = AddressGuard(resolver, check=_admitting(_PUBLIC))
        async with serving() as server, httpx2.AsyncClient(transport=_pinned(guard)) as client:
            responses = await asyncio.gather(*(client.get(f"http://api.example:{server.port}/{n}") for n in range(2)))

        assert [response.status_code for response in responses] == [httpx2.codes.OK] * 2
        assert sorted(seen.path for seen in server.requests) == ["/0", "/1"]
        assert resolver.lookups == ["api.example"] * 2

    async def test_a_guarded_client_looks_each_hop_up_once(self) -> None:
        """Skip the request hook's lookup over a curl transport, which vets and pins in the same step."""
        resolver = _Resolver(_PUBLIC)
        security = SecurityOptions(block_private_hosts=True, resolver=resolver, check=_admitting(_PUBLIC))
        async with serving() as server:
            options = HttpOptions(transport=AsyncCurlTransport(impersonate="chrome"), security=security)
            client = Client(
                base_url=f"http://api.example:{server.port}",
                policy=CallPolicy(rate=Rate.per_second(1000), retry=None),
                http=options,
            )
            async with client:
                await client.get("x", expect=RESPONSE)

        assert resolver.lookups == ["api.example"]


async def test_a_lookup_counts_against_the_connect_timeout() -> None:
    """Give up a lookup that outlasts the connect timeout, as curl's own lookup would."""

    async def hanging(_host: str, _port: int) -> Sequence[str]:
        await asyncio.Event().wait()
        return ()

    timeout = httpx2.Timeout(5.0, connect=0.05)
    async with httpx2.AsyncClient(transport=_pinned(AddressGuard(hanging)), timeout=timeout) as client:
        with pytest.raises(httpx2.ConnectTimeout, match=r"resolving api\.example timed out"):
            await client.get("http://api.example/x")


async def test_a_name_that_does_not_resolve_fails_as_a_connect_error() -> None:
    """Raise the connect error a name that does not resolve raises unguarded."""

    async def unknown(host: str, _port: int) -> Sequence[str]:
        raise OSError(host)

    async with httpx2.AsyncClient(transport=_pinned(AddressGuard(unknown))) as client:
        with pytest.raises(httpx2.ConnectError, match=r"could not resolve api\.example"):
            await client.get("http://api.example/x")


async def test_a_failed_lookup_leaves_a_one_shot_body_unread() -> None:
    """Vet before reading the body, so a lookup that fails has consumed nothing a retry would need."""
    read: list[bytes] = []

    async def body() -> AsyncIterator[bytes]:
        read.append(b"order")
        yield b"order"

    async def unknown(host: str, _port: int) -> Sequence[str]:
        raise OSError(host)

    async with httpx2.AsyncClient(transport=_pinned(AddressGuard(unknown))) as client:
        with pytest.raises(httpx2.ConnectError):
            await client.post("http://api.example/x", content=body())

    assert read == []


def _client(transport: AsyncCurlTransport, security: SecurityOptions | None = None) -> Client:
    """Build a client over *transport*, guarded by *security* when given."""
    http = HttpOptions(transport=transport, security=security or SecurityOptions())
    return Client(base_url="https://api.example", policy=CallPolicy(rate=Rate.per_second(1000)), http=http)


async def _resolve(_host: str, _port: int) -> Sequence[str]:
    """Answer every name with a public address."""
    return ("93.184.216.34",)


async def _resolve_otherwise(_host: str, _port: int) -> Sequence[str]:
    """Answer every name with another public address."""
    return ("93.184.216.35",)


_GUARDED: Final = SecurityOptions(block_private_hosts=True)


def _admit_all(_address: str) -> str | None:
    """Admit every address."""
    return None


@pytest.mark.parametrize(
    ("first", "second"),
    [
        pytest.param(_GUARDED, None, id="unguarded-after-guarded"),
        pytest.param(None, _GUARDED, id="guarded-after-unguarded"),
        pytest.param(
            SecurityOptions(block_private_hosts=True, resolver=_resolve),
            SecurityOptions(block_private_hosts=True, resolver=_resolve_otherwise),
            id="another-resolver",
        ),
        pytest.param(_GUARDED, SecurityOptions(block_private_hosts=True, check=_admit_all), id="another-check"),
    ],
)
async def test_a_transport_serving_one_guard_refuses_a_client_with_another(
    first: SecurityOptions | None, second: SecurityOptions | None
) -> None:
    """Refuse, at construction, a client whose guard differs from the one the transport already serves."""
    transport = AsyncCurlTransport(impersonate="chrome")
    async with _client(transport, first):
        with pytest.raises(ValueError, match="already serves a client with a different SSRF guard"):
            _client(transport, second)


async def test_a_copy_of_a_transport_serves_its_guard() -> None:
    """Refuse a guarded client on a copy of a transport an unguarded one uses: the copy shares curl's session."""
    transport = AsyncCurlTransport(impersonate="chrome")
    async with _client(transport):
        with pytest.raises(ValueError, match="already serves a client with a different SSRF guard"):
            _client(copy.copy(transport), _GUARDED)


def test_a_wrapper_cannot_fake_a_binding() -> None:
    """Read a transport's binding from its own attributes, so a wrapper forwarding attributes has none of its own."""

    class Forwarding(httpx2.AsyncBaseTransport):
        def __init__(self, inner: AsyncCurlTransport) -> None:
            self.inner = inner

        def __getattr__(self, name: str) -> object:
            return getattr(self.inner, name)

    assert guard_binding(Forwarding(AsyncCurlTransport(impersonate="chrome"))) is None
    assert guard_binding(AsyncCurlTransport(impersonate="chrome")) is not None


def test_a_subclass_is_refused_with_what_to_write_instead() -> None:
    """Refuse a subclass, pointing to a middleware for a change to each request, and saying a wrapper is not pinned."""
    message = (
        r"^_Logged cannot subclass AsyncCurlTransport, which is final: change each request in a middleware "
        r"\(Client\(middleware=\.\.\.\)\), which runs on every attempt; a transport of your own that sends through it "
        r"is not pinned by the SSRF guard, which only vets each hop before it is sent$"
    )
    with pytest.raises(TypeError, match=message):
        types.new_class("_Logged", (cast("Any", AsyncCurlTransport),))


def test_an_object_without_attributes_of_its_own_has_no_binding() -> None:
    """Find no binding on an object with no instance dictionary, rather than fail to look."""
    assert guard_binding(cast("httpx2.AsyncBaseTransport", object())) is None


@pytest.mark.parametrize(
    "security",
    [
        pytest.param(_GUARDED, id="default-resolver"),
        pytest.param(SecurityOptions(block_private_hosts=True, resolver=_resolve), id="one-resolver"),
        pytest.param(None, id="unguarded"),
    ],
)
async def test_clients_with_the_same_guard_may_share_a_transport(security: SecurityOptions | None) -> None:
    """Accept a second client whose guard is the one the transport already serves."""
    transport = AsyncCurlTransport(impersonate="chrome")
    async with _client(transport, security), _client(transport, security):
        pass


@pytest.mark.parametrize(
    ("option", "value"),
    [
        pytest.param(CurlOpt.PROXY, "http://proxy.example:3128", id="PROXY"),
        pytest.param(CurlOpt.PRE_PROXY, "socks5://proxy.example:1080", id="PRE_PROXY"),
        pytest.param(CurlOpt.RESOLVE, ["api.example:443:10.0.0.5"], id="RESOLVE"),
        pytest.param(CurlOpt.CONNECT_TO, ["api.example:443:10.0.0.5:443"], id="CONNECT_TO"),
        pytest.param(CurlOpt.UNIX_SOCKET_PATH, "/run/docker.sock", id="UNIX_SOCKET_PATH"),
        pytest.param(CurlOpt.ABSTRACT_UNIX_SOCKET, "internal", id="ABSTRACT_UNIX_SOCKET"),
        pytest.param(CurlOpt.PORT, 6379, id="PORT"),
    ],
)
async def test_a_curl_option_choosing_the_address_is_refused_with_the_guard(option: CurlOpt, value: object) -> None:
    """Refuse, at construction, the guard over curl options that choose the address the connection reaches."""
    transport = AsyncCurlTransport(impersonate="chrome", curl_options={option: value})
    with pytest.raises(ValueError, match=f"cannot be combined with curl_options {option.name}"):
        _client(transport, _GUARDED)
    async with _client(transport):  # the refusal bound nothing: the transport still serves an unguarded client
        pass
