# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""``HttpOptions(allow_private_hosts=False)``: a client refuses every hop to a non-global address.

Over the default transport the refusal comes from the connection itself (nothing reaches the server); over a custom
transport (a test double, the curl transport) a request hook refuses the hop before it is sent. Either way the call
raises ``ForbiddenDestinationError``, which is not retried and not charged to the breaker.
"""

from __future__ import annotations

import socket
from typing import TYPE_CHECKING, Final

import pytest

from headgate import BreakerPolicy, ForbiddenDestinationError, HttpOptions, RetryPolicy, TransportError, TransportPolicy
from headgate.breaker import BreakerRegistry
from headgate.curl import AsyncCurlTransport
from headgate.ratelimit import Rate
from tests.security._servers import serving
from tests.security._web import BASE, Web
from tests.support.clients import StubClient
from tests.support.responses import RESPONSE

if TYPE_CHECKING:
    from collections.abc import Sequence

#: An address standing in for any public one; the custom-transport tests never connect to it.
_PUBLIC: Final = "93.184.216.34"


class _Names:
    """A resolver over a fixed table of names, changeable mid-test to simulate rebinding."""

    def __init__(self, **table: str) -> None:
        """Resolve each name (underscores read as dots) to its address."""
        self.table = {name.replace("_", "."): address for name, address in table.items()}

    async def __call__(self, host: str, _port: int) -> Sequence[str]:
        """Return *host*'s address, or fail the lookup as an unknown name does."""
        if host not in self.table:
            msg = f"unknown name {host}"
            raise socket.gaierror(msg)
        return (self.table[host],)


def _guarded(base_url: str, policy: TransportPolicy | None = None) -> StubClient:
    """Build a client on *base_url* with the SSRF guard on, under *policy* (the default one if not given)."""
    return StubClient(
        base_url=base_url,
        rate=Rate.per_second(1000),
        policy=policy or TransportPolicy(),
        http=HttpOptions(allow_private_hosts=False),
    )


async def test_a_loopback_server_is_never_reached() -> None:
    """Refuse a call to a loopback address before connecting, with the call's details and the reason."""
    async with serving() as server, _guarded(server.url()) as client:
        with pytest.raises(ForbiddenDestinationError) as exc:
            await client.get("x", expect=RESPONSE)

    assert server.connections == 0
    error = exc.value
    assert str(error).startswith("[StubClient] GET x: refused to connect to 127.0.0.1: it is a loopback address")
    assert (error.source, error.method, error.url, error.retryable) == ("StubClient", "GET", "x", False)


async def test_the_same_server_is_reachable_without_the_guard() -> None:
    """Reach the same loopback server when private hosts are allowed, the default."""
    async with serving() as server, StubClient(base_url=server.url(), rate=Rate.per_second(1000)) as client:
        resp = await client.get("x", expect=RESPONSE)

    assert resp.text == "ok"


async def test_a_refusal_is_not_retried_and_not_charged_to_the_breaker() -> None:
    """Send no retry for a refusal and leave the breaker closed, however often it happens."""
    breaker = BreakerPolicy(failure_threshold=1, registry=BreakerRegistry())
    async with (
        serving() as server,
        _guarded(server.url(), TransportPolicy(retry=RetryPolicy(attempts=3), breaker=breaker)) as client,
    ):
        for _ in range(3):
            with pytest.raises(ForbiddenDestinationError) as exc:
                await client.get("x", expect=RESPONSE)
            assert exc.value.attempts == 1


async def test_a_name_resolving_to_a_private_address_is_refused() -> None:
    """Resolve through ``HttpOptions.resolver`` and refuse a name that answers with a private address."""
    options = HttpOptions(allow_private_hosts=False, resolver=_Names(internal_example="10.0.0.5"))
    async with StubClient(base_url="https://internal.example", rate=Rate.per_second(1000), http=options) as client:
        with pytest.raises(ForbiddenDestinationError, match=r"internal\.example: it resolves to 10\.0\.0\.5"):
            await client.get("x", expect=RESPONSE)


def _web_client(web: Web, resolver: _Names) -> StubClient:
    """Return a guarded client over *web*, a custom transport, resolving through *resolver*."""
    return web.client(http=HttpOptions(allow_private_hosts=False, resolver=resolver))


@pytest.mark.parametrize(
    "target",
    ["https://internal.example/admin", "https://169.254.169.254/latest/meta-data/", "https://[::ffff:127.0.0.1]/"],
    ids=["private-name", "metadata-literal", "mapped-loopback"],
)
async def test_a_redirect_to_a_private_host_is_refused_over_a_custom_transport(target: str) -> None:
    """Vet every hop as a request hook over a custom transport: a redirect inward is refused before it is sent."""
    web = Web({f"{BASE}/start": target})
    async with _web_client(web, _Names(api_example=_PUBLIC, internal_example="10.1.2.3")) as client:
        with pytest.raises(ForbiddenDestinationError):
            await client.get("start", expect=RESPONSE)

    assert [str(request.url) for request in web.sent] == [f"{BASE}/start"]


async def test_a_rebinding_name_is_refused_on_the_next_hop_over_a_custom_transport() -> None:
    """Look the name up again on every hop, so a name that rebinds inward is refused from then on."""
    web = Web()
    names = _Names(api_example=_PUBLIC)
    async with _web_client(web, names) as client:
        await client.get("first", expect=RESPONSE)
        names.table["api.example"] = "127.0.0.1"
        with pytest.raises(ForbiddenDestinationError):
            await client.get("second", expect=RESPONSE)

    assert len(web.sent) == 1


async def test_an_unknown_name_is_a_transport_error_over_a_custom_transport() -> None:
    """Raise a failed lookup as the retryable ``TransportError`` the transport itself would raise."""
    async with _web_client(Web(), _Names()) as client:
        with pytest.raises(TransportError, match=r"could not resolve api\.example") as exc:
            await client.get("x", expect=RESPONSE)

    assert exc.value.retryable


async def test_the_curl_transport_is_vetted_before_it_sends() -> None:
    """Refuse a loopback hop over the curl transport before curl connects (curl resolves names itself, unpinned)."""
    async with serving() as server:
        options = HttpOptions(allow_private_hosts=False, transport=AsyncCurlTransport(impersonate="chrome"))
        async with StubClient(base_url=server.url(), rate=Rate.per_second(1000), http=options) as client:
            with pytest.raises(ForbiddenDestinationError):
                await client.get("x", expect=RESPONSE)

    assert server.connections == 0


@pytest.mark.parametrize(
    ("options", "message"),
    [
        (lambda: HttpOptions(resolver=_Names()), "needs allow_private_hosts=False"),
        (lambda: HttpOptions(allow_private_hosts=False, proxy="http://proxy.example:3128"), "proxy"),
    ],
    ids=["resolver-without-guard", "guard-with-proxy"],
)
def test_a_guard_setting_that_cannot_work_is_refused(options: object, message: str) -> None:
    """Refuse a resolver that nothing would use, and the guard behind a proxy, which resolves past it."""
    assert callable(options)
    with pytest.raises(ValueError, match=message):
        options()
