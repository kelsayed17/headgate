# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""``fake_network``: every client built in its block sends on the test's transport, the rest of its stack its own."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final, cast, override

import httpx2
import pytest

from headgate import CallPolicy, Client, HttpOptions, Rate, SecurityOptions, TransportError, nothing
from headgate.curl import AsyncCurlTransport
from headgate.errors import ForbiddenDestinationError
from headgate.testing import FakeNetwork, FakeTransport, TestClock, budget_for, fake_network

if TYPE_CHECKING:
    from headgate.security import Resolver

_RATE: Final = Rate.per_second(50)


class _Sdk:
    """An SDK that holds a ``Client`` and fixes its transport, as one built for production would."""

    def __init__(self, transport: httpx2.AsyncBaseTransport | None = None, rate: Rate = _RATE) -> None:
        """Hold a client on *transport*, else on curl, paced at *rate*."""
        self.http = Client(
            base_url="https://api.example.test/v1",
            policy=CallPolicy(rate=rate),
            http=HttpOptions(transport=AsyncCurlTransport(impersonate="chrome") if transport is None else transport),
        )

    async def ping(self) -> None:
        """Make one call."""
        await self.http.get("ping", expect=nothing)


async def test_an_sdk_built_in_the_block_sends_on_the_fake() -> None:
    """Send on the fake, though the SDK fixed a curl transport of its own, and yield a handle holding the fake."""
    fake = FakeTransport().reply("GET", "/v1/ping", 200)
    with fake_network(fake) as net:
        sdk = _Sdk()
    await sdk.ping()

    assert net.transport is fake
    assert [call.url.path for call in fake.calls] == ["/v1/ping"]


def test_the_handle_holds_the_clients_built_in_the_block() -> None:
    """Hold each client built in the block, in order: not a view, not one whose construction failed, not one after."""
    with fake_network(FakeTransport()) as net:
        first, second = _Sdk(), _Sdk()
        first.http.with_options(deadline=5.0)
        with pytest.raises(TypeError):
            Client(base_url="https://api.example.test/", policy=cast("Any", "fast"))
    _Sdk()

    assert net.clients == (first.http, second.http)
    assert isinstance(net.clients, tuple)
    assert repr(net) == "FakeNetwork(FakeTransport, clients=2)"


def test_the_handle_reads_an_sdks_configuration_back() -> None:
    """Read an SDK's rate budget through the client the handle holds, without its private attribute."""
    with fake_network(FakeTransport()) as net:
        _Sdk(rate=Rate.per_second(7))
    [client] = net.clients

    assert budget_for(client).rate == Rate.per_second(7)


def _guarded(resolver: Resolver | None = None) -> Client:
    """Build a client on a real-looking host, with the SSRF guard on and, when given, a resolver of its own."""
    return Client(
        base_url="https://api.example.test/v1",
        policy=CallPolicy(rate=_RATE),
        http=HttpOptions(security=SecurityOptions(block_private_hosts=True, resolver=resolver)),
    )


async def _never_resolve(host: str, port: int) -> list[str]:
    """Fail the test: under the fake network, the SDK's own resolver is never asked."""
    raise AssertionError((host, port))


@pytest.mark.parametrize("resolver", [None, _never_resolve], ids=["system", "own"])
async def test_a_guarded_sdk_resolves_its_host_inside_the_fake_network(resolver: Resolver | None) -> None:
    """Resolve the SDK's host to a public address the guard admits, with no DNS lookup and not through its resolver."""
    fake = FakeTransport().reply("GET", "/v1/ping", 200)
    with fake_network(fake):
        client = _guarded(resolver)
    await client.get("ping", expect=nothing)

    assert len(fake.calls) == 1


async def test_a_host_the_fake_resolves_to_a_private_address_is_refused() -> None:
    """Refuse a host the test resolves to a private address, as the guard would on the real network."""
    fake = FakeTransport().reply("GET", "/v1/ping", 200).resolve("API.example.test", "8.8.4.4", "10.0.0.1")
    with fake_network(fake):
        client = _guarded()

    with pytest.raises(ForbiddenDestinationError, match=r"resolves to 10\.0\.0\.1, which is a private address"):
        await client.get("ping", expect=nothing)
    assert not fake.calls


async def test_a_host_the_fake_resolves_to_nothing_does_not_resolve() -> None:
    """Fail as a name that does not resolve when the test resolves the host to no address."""
    fake = FakeTransport().reply("GET", "/v1/ping", 200).resolve("api.example.test")
    with fake_network(fake):
        client = _guarded()

    with pytest.raises(TransportError, match="resolved to no address"):
        await client.get("ping", expect=nothing)
    assert not fake.calls


async def test_an_sdk_built_outside_the_block_keeps_its_own_transport() -> None:
    """Leave a client built after the block on the transport it chose."""
    own, fake = FakeTransport().reply("GET", "/v1/ping", 200), FakeTransport()
    with fake_network(fake):
        pass
    sdk = _Sdk(own)
    await sdk.ping()

    assert (len(own.calls), len(fake.calls)) == (1, 0)


@pytest.mark.call_policy
async def test_the_sdks_retries_and_rate_budget_run_over_the_fake() -> None:
    """Replace the network only: the client still retries a 503, and paces its sends at its own rate."""
    fake = FakeTransport().reply("GET", "/v1/ping", 503, 200)
    clock = TestClock(random=lambda: 0.0)
    with fake_network(fake), clock.install():
        sdk = _Sdk(rate=Rate.per_second(1))
    await sdk.ping()
    await sdk.ping()

    assert len(fake.calls) == len(("503", "200", "200"))
    assert sum(clock.sleeps) >= 1.0, "the sends waited their turns in the budget"


async def test_the_ssrf_guard_still_refuses_a_private_address() -> None:
    """Keep the SSRF guard of a client that blocks private hosts: the fake replaces the network, not the guard."""
    fake = FakeTransport().reply("GET", "/x", 200)
    with fake_network(fake):
        client = Client(
            base_url="http://127.0.0.1",
            policy=CallPolicy(rate=_RATE),
            http=HttpOptions(security=SecurityOptions(block_private_hosts=True)),
        )

    with pytest.raises(ForbiddenDestinationError):
        await client.get("x", expect=nothing)
    assert not fake.calls


@pytest.mark.parametrize(
    ("addresses", "error", "message"),
    [
        (
            ("localhost",),
            ValueError,
            r"^resolve\(\) takes IP addresses as text, such as '10\.0\.0\.1' \(got 'localhost'\)$",
        ),
        ((167772161,), TypeError, r"^addresses= takes IP addresses as text, such as '10\.0\.0\.1' \(got int\)$"),
    ],
    ids=["name", "int"],
)
def test_a_fake_resolves_only_ip_addresses(addresses: tuple[object, ...], error: type[Exception], message: str) -> None:
    """Refuse an address that is no IP address, naming it, where the test writes it."""
    with pytest.raises(error, match=message):
        FakeTransport().resolve("api.example.test", *cast("tuple[str, ...]", addresses))


def test_a_fake_resolves_a_host_given_as_text() -> None:
    """Refuse a host that is no ``str``."""
    with pytest.raises(TypeError, match=r"^host= takes a host name, a str \(got bytes\)$"):
        FakeTransport().resolve(cast("Any", b"api.example.test"), "8.8.4.4")


@dataclass
class _Recording(httpx2.AsyncBaseTransport):
    """A transport of the test's own, a dataclass, so unhashable."""

    seen: list[httpx2.URL] = field(default_factory=list)

    @override
    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        self.seen.append(request.url)
        return httpx2.Response(200)


async def test_a_guarded_client_on_an_unhashable_transport_resolves_inside_the_fake_network() -> None:
    """Resolve names over a transport that is no ``FakeTransport``, and cannot be hashed, to the public fake address."""
    transport = _Recording()
    with fake_network(transport):
        client = _guarded()
    await client.get("ping", expect=nothing)

    assert len(transport.seen) == 1


@pytest.mark.parametrize("host", ["localhost", "api.localhost", "LOCALHOST"])
async def test_localhost_resolves_to_loopback_inside_the_fake_network(host: str) -> None:
    """Resolve ``localhost`` and its subdomains to loopback (RFC 6761), so the guard refuses them as in production."""
    fake = FakeTransport().reply("GET", "/v1/ping", 200)
    with fake_network(fake):
        client = Client(
            base_url=f"https://{host}/v1",
            policy=CallPolicy(rate=_RATE),
            http=HttpOptions(security=SecurityOptions(block_private_hosts=True)),
        )

    with pytest.raises(ForbiddenDestinationError, match=r"resolves to 127\.0\.0\.1, which is a loopback address"):
        await client.get("ping", expect=nothing)
    assert not fake.calls


async def test_a_localhost_the_test_resolved_takes_its_answer() -> None:
    """Take the test's own answer for a ``localhost`` name, as for any other."""
    fake = FakeTransport().reply("GET", "/v1/ping", 200).resolve("api.localhost", "8.8.4.4")
    with fake_network(fake):
        client = Client(
            base_url="https://api.localhost/v1",
            policy=CallPolicy(rate=_RATE),
            http=HttpOptions(security=SecurityOptions(block_private_hosts=True)),
        )
    await client.get("ping", expect=nothing)

    assert len(fake.calls) == 1


def test_a_fake_network_comes_only_from_fake_network() -> None:
    """Refuse to build the handle directly: ``fake_network`` yields it."""
    with pytest.raises(TypeError, match=r"^FakeNetwork is yielded by fake_network\(transport\): write with "):
        FakeNetwork()
    with pytest.raises(TypeError, match=r"^FakeNetwork is yielded by fake_network\(transport\)"):
        FakeNetwork(cast("Any", FakeTransport()))
