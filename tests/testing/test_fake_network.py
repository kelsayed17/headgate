# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""``fake_network``: every client built in its block sends on the test's transport, the rest of its stack its own."""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final, cast, override

import httpx2
import pytest

from headgate import CallPolicy, Client, ClientError, HttpOptions, Rate, SecurityOptions, TransportError, nothing
from headgate._stack import NETWORK_OVERRIDE, NetworkOverride
from headgate.curl import AsyncCurlTransport
from headgate.errors import ForbiddenDestinationError
from headgate.testing import (
    FakeNetwork,
    FakeTransport,
    FaultyTransport,
    Respond,
    TestClock,
    UnmatchedRequestError,
    budget_for,
    fake_network,
)
from tests.security._servers import serving

if TYPE_CHECKING:
    from collections.abc import Callable

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


async def test_a_host_the_network_resolves_to_a_private_address_is_refused() -> None:
    """Refuse a host the test resolves to a private address, as the guard would on the real network."""
    fake = FakeTransport().reply("GET", "/v1/ping", 200)
    with fake_network(fake) as net:
        net.resolve("API.example.test", "8.8.4.4", "10.0.0.1")
        client = _guarded()

    with pytest.raises(ForbiddenDestinationError, match=r"resolves to 10\.0\.0\.1, which is a private address"):
        await client.get("ping", expect=nothing)
    assert not fake.calls


@pytest.mark.parametrize(
    ("addresses", "sent"), [(("8.8.4.4",), 1), (("8.8.4.4", "10.0.0.1"), 0)], ids=["public", "private"]
)
async def test_a_guarded_sdk_on_curl_is_guarded_on_the_fake_network(addresses: tuple[str, ...], sent: int) -> None:
    """Guard an SDK that fixed a curl transport as one on the default transport: its host is checked, then faked."""
    fake = FakeTransport().reply("GET", "/v1/ping", 200)
    with fake_network(fake) as net:
        net.resolve("api.example.test", *addresses)
        client = Client(
            base_url="https://api.example.test/v1",
            policy=CallPolicy(rate=_RATE),
            http=HttpOptions(
                transport=AsyncCurlTransport(impersonate="chrome"), security=SecurityOptions(block_private_hosts=True)
            ),
        )

    if sent:
        await client.get("ping", expect=nothing)
    else:
        with pytest.raises(ForbiddenDestinationError, match=r"resolves to 10\.0\.0\.1, which is a private address"):
            await client.get("ping", expect=nothing)
    assert len(fake.calls) == sent


@pytest.mark.parametrize(
    "transport",
    [httpx2.AsyncHTTPTransport, lambda: AsyncCurlTransport(impersonate="chrome")],
    ids=["httpx2", "curl"],
)
def test_a_transport_that_sends_on_the_network_is_refused(transport: Callable[[], httpx2.AsyncBaseTransport]) -> None:
    """Refuse a real network transport where the block is entered: the guard would vet names it never looks up."""
    block = fake_network(transport())
    with pytest.raises(TypeError, match=r"^transport= takes a fake, which answers in place of the network, such as "):
        block.__enter__()


async def test_the_request_hooks_vet_every_hop_over_a_test_network_that_could_pin() -> None:
    """Vet over a test's network through the request hooks: it is never bound to the guard, so it never pins.

    Only a curl transport could pin, and ``fake_network`` refuses one, so the override is set directly here.
    """
    async with serving() as server:
        token = NETWORK_OVERRIDE.set(NetworkOverride(AsyncCurlTransport(impersonate="chrome"), _never_resolve))
        try:
            client = _guarded()
        finally:
            NETWORK_OVERRIDE.reset(token)
        async with client:
            with pytest.raises(ForbiddenDestinationError, match=r"to 127\.0\.0\.1: it is a loopback address"):
                await client.get(httpx2.URL(server.url("/x")), expect=nothing)

    assert server.connections == 0


async def test_a_host_the_network_resolves_to_nothing_does_not_resolve() -> None:
    """Fail as a name that does not resolve when the test resolves the host to no address."""
    fake = FakeTransport().reply("GET", "/v1/ping", 200)
    with fake_network(fake) as net:
        net.resolve("api.example.test")
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
            r"^resolve\(\*addresses\) takes IP addresses as text, such as '10\.0\.0\.1' \(got 'localhost'\)$",
        ),
        (
            (167772161,),
            TypeError,
            r"^resolve\(\*addresses\) takes IP addresses as text, such as '10\.0\.0\.1' \(got int\)$",
        ),
    ],
    ids=["name", "int"],
)
def test_the_network_resolves_only_ip_addresses(
    addresses: tuple[object, ...], error: type[Exception], message: str
) -> None:
    """Refuse an address that is no IP address, naming it, where the test writes it."""
    with fake_network(FakeTransport()) as net, pytest.raises(error, match=message):
        net.resolve("api.example.test", *cast("tuple[str, ...]", addresses))


def test_the_network_resolves_a_host_given_as_text() -> None:
    """Refuse a host that is no ``str``."""
    with (
        fake_network(FakeTransport()) as net,
        pytest.raises(TypeError, match=r"^host= takes a host name, a str \(got bytes\)$"),
    ):
        net.resolve(cast("Any", b"api.example.test"), "8.8.4.4")


def test_names_are_the_networks_not_the_transports() -> None:
    """Resolve names on the handle alone: a ``FakeTransport`` answers requests, and has no ``resolve`` of its own."""
    assert not hasattr(FakeTransport, "resolve")


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


async def test_the_network_resolves_a_name_whatever_transport_it_is_given() -> None:
    """Take the test's answer over a transport that is no ``FakeTransport``, given before or after the client is built.

    A wrapper of the fake, kept to log what reaches the network, is such a transport: its private hosts stay refused.
    """
    transport = _Recording()
    with fake_network(transport) as net:
        client = _guarded()
    net.resolve("api.example.test", "10.0.0.7")

    with pytest.raises(ForbiddenDestinationError, match=r"resolves to 10\.0\.0\.7, which is a private address"):
        await client.get("ping", expect=nothing)
    assert not transport.seen


@pytest.mark.parametrize(
    ("resolved", "asked"),
    [
        ("internal.example", "internal.example."),
        ("internal.example.", "internal.example"),
        ("Internal.Example", "internal.example"),
        ("bücher.example", "bücher.example"),
        ("bücher.example", "xn--bcher-kva.example"),
        ("xn--bcher-kva.example", "BÜCHER.example."),
    ],
    ids=["dot-asked", "dot-resolved", "case", "unicode", "unicode-resolved", "punycode-resolved"],
)
async def test_a_host_takes_its_answer_however_it_is_spelled(resolved: str, asked: str) -> None:
    """Match a resolved host as the guard asks for it: lower case, as IDNA's ASCII, with or without one final dot.

    A name is one name in any of those spellings, so a test's private answer cannot be missed by spelling it another
    way, and the guard admit the host as a public one.
    """
    fake = FakeTransport().reply("GET", "/x", 200)
    with fake_network(fake) as net:
        net.resolve(resolved, "10.0.0.7")
        client = _guarded()

    with pytest.raises(ForbiddenDestinationError, match=r"resolves to 10\.0\.0\.7, which is a private address"):
        await client.get(httpx2.URL(f"https://{asked}/x"), expect=nothing)
    assert not fake.calls


def test_a_host_that_is_no_name_is_refused() -> None:
    """Refuse a host no URL can hold, as IDNA refuses it, where the test resolves it."""
    with (
        fake_network(FakeTransport()) as net,
        pytest.raises(ValueError, match=r"^host= takes a host name, a str \(got '☃\.example'\): Invalid IDNA hostname"),
    ):
        net.resolve("☃.example", "10.0.0.7")


@pytest.mark.parametrize("host", ["localhost", "api.localhost", "LOCALHOST", "localhost.", "api.localhost."])
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
    fake = FakeTransport().reply("GET", "/v1/ping", 200)
    with fake_network(fake) as net:
        net.resolve("api.localhost", "8.8.4.4")
        client = Client(
            base_url="https://api.localhost/v1",
            policy=CallPolicy(rate=_RATE),
            http=HttpOptions(security=SecurityOptions(block_private_hosts=True)),
        )
    await client.get("ping", expect=nothing)

    assert len(fake.calls) == 1


def test_a_fake_network_comes_only_from_fake_network() -> None:
    """Refuse to build the handle directly, whatever it is given, and show no parameters: ``fake_network`` yields it."""
    assert inspect.signature(FakeNetwork) == inspect.Signature()
    with pytest.raises(TypeError, match=r"^FakeNetwork is yielded by fake_network\(transport\): write with "):
        FakeNetwork()
    with pytest.raises(TypeError, match=r"^FakeNetwork is yielded by fake_network\(transport\)"):
        FakeNetwork(cast("Any", FakeTransport()))


#: What a call of a client built outside every fake network's block says, inside one.
_BUILT_OUTSIDE = (
    r"^GET ping: refused to send: this client was built before the fake_network block was entered, so it would send "
    r"on its own network, not the block's FakeTransport: build the SDK inside the block$"
)


async def test_an_sdk_built_before_the_block_refuses_to_send_inside_it() -> None:
    """Refuse a call of a client built before the block on the network, its own curl or the default, sending nothing."""
    sdk, on_the_network = _Sdk(), Client(base_url="https://api.example.test/v1", policy=CallPolicy(rate=_RATE))
    fake = FakeTransport().reply("GET", "/v1/ping", 200)
    with fake_network(fake):
        for client in (sdk.http, sdk.http.with_options(deadline=5.0), on_the_network):
            with pytest.raises(UnmatchedRequestError, match=_BUILT_OUTSIDE) as caught:
                await client.get("ping", expect=nothing)
            assert not isinstance(caught.value, ClientError), "an SDK's own except ClientError must not swallow it"

    assert fake.calls == ()


@pytest.mark.parametrize(
    "own",
    [lambda: FakeTransport().reply("GET", "/v1/ping", 200), lambda: FaultyTransport(schedule=[Respond(200)] * 2)],
    ids=["fake", "faulty"],
)
async def test_an_sdk_built_before_the_block_on_a_fake_of_its_own_sends_on_it(
    own: Callable[[], FakeTransport | FaultyTransport],
) -> None:
    """Send a call of a client built before the block on the testing kit's own transport, which reaches no network."""
    transport, fake = own(), FakeTransport()
    sdk = _Sdk(transport)
    with fake_network(fake):
        await sdk.ping()
        await sdk.http.with_options(deadline=5.0).get("ping", expect=nothing)

    assert (len(transport.calls), fake.calls) == (2, ())


async def test_an_sdk_built_in_another_block_sends_on_its_own_fake() -> None:
    """Send a call of a client built in another block on that block's fake, as a block nested in a fixture's does."""
    outer, inner = FakeTransport(), FakeTransport().reply("GET", "/v1/ping", 200)
    with fake_network(outer):
        with fake_network(inner):
            sdk = _Sdk()
        await sdk.ping()

    assert (len(outer.calls), len(inner.calls)) == (0, 1)


async def test_an_sdk_built_on_the_blocks_own_transport_sends_inside_it() -> None:
    """Send a call of a client built before the block on the very transport the block puts clients on."""
    fake = FakeTransport().reply("GET", "/v1/ping", 200)
    sdk = _Sdk(fake)
    with fake_network(fake):
        await sdk.ping()

    assert len(fake.calls) == 1
