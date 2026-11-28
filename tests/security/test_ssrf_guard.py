# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""``SecurityOptions(block_private_hosts=True)``: a client refuses every hop to a non-global address.

Over the default transport the refusal comes from the connection itself (nothing reaches the server); over a custom
transport (a test double, the curl transport) a request hook refuses the hop before it is sent. Either way the call
raises ``ForbiddenDestinationError``, which is not retried and not charged to the breaker.
"""

from __future__ import annotations

import asyncio
import dataclasses
import socket
from typing import TYPE_CHECKING, Any, Final, cast, override

import httpx2
import pytest

import headgate.security
from headgate import (
    BreakerPolicy,
    CallPolicy,
    Client,
    HttpOptions,
    Next,
    Rate,
    RetryPolicy,
    SecurityOptions,
    TransportError,
)
from headgate.breaker import BreakerRegistry
from headgate.curl import AsyncCurlTransport
from headgate.errors import ForbiddenDestinationError
from headgate.health import breaker_snapshots
from headgate.retry import Hedge
from headgate.security import forbidden_reason
from headgate.testing import FakeTransport, fake_network
from tests.security._servers import serving
from tests.security._web import BASE, Web
from tests.support.responses import RESPONSE

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from headgate import Middleware
    from headgate.security import AddressCheck

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


def _guarded(base_url: str, policy: CallPolicy | None = None) -> Client:
    """Build a client on *base_url* with the SSRF guard on, under *policy* (the default one if not given)."""
    return Client(
        base_url=base_url,
        policy=dataclasses.replace(policy or CallPolicy(), rate=Rate.per_second(1000)),
        http=HttpOptions(security=SecurityOptions(block_private_hosts=True)),
    )


async def test_a_loopback_server_is_never_reached() -> None:
    """Refuse a call to a loopback address before connecting, with the call's details and the reason."""
    async with serving() as server, _guarded(server.url()) as client:
        with pytest.raises(ForbiddenDestinationError) as exc:
            await client.get("x", expect=RESPONSE)

    assert server.connections == 0
    error = exc.value
    assert str(error).startswith(f"[{error.source}] GET x: refused to connect to 127.0.0.1: it is a loopback address")
    assert error.source == httpx2.URL(server.url()).netloc.decode()
    assert (error.method, error.url, error.retryable) == ("GET", "x", False)


async def test_the_same_server_is_reachable_without_the_guard() -> None:
    """Reach the same loopback server when private hosts are allowed, the default."""
    async with (
        serving() as server,
        Client(base_url=server.url(), policy=CallPolicy(rate=Rate.per_second(1000))) as client,
    ):
        resp = await client.get("x", expect=RESPONSE)

    assert resp.text == "ok"


async def test_a_refusal_is_not_retried_and_not_charged_to_the_breaker() -> None:
    """Send no retry for a refusal and leave the breaker closed, however often it happens."""
    breaker = BreakerRegistry(BreakerPolicy(failure_threshold=1))
    async with (
        serving() as server,
        _guarded(server.url(), CallPolicy(retry=RetryPolicy(attempts=3), breaker=breaker)) as client,
    ):
        for _ in range(3):
            with pytest.raises(ForbiddenDestinationError) as exc:
                await client.get("x", expect=RESPONSE)
            assert exc.value.attempts == 1


async def test_a_name_resolving_to_a_private_address_is_refused() -> None:
    """Resolve through ``HttpOptions.resolver`` and refuse a name that answers with a private address."""
    options = HttpOptions(
        security=SecurityOptions(block_private_hosts=True, resolver=_Names(internal_example="10.0.0.5"))
    )
    async with Client(
        base_url="https://internal.example", policy=CallPolicy(rate=Rate.per_second(1000)), http=options
    ) as client:
        with pytest.raises(ForbiddenDestinationError, match=r"internal\.example: it resolves to 10\.0\.0\.5"):
            await client.get("x", expect=RESPONSE)


def _web_client(web: Web, resolver: _Names, *, base_url: str = BASE) -> Client:
    """Return a guarded client on *base_url* over *web*, a custom transport, resolving through *resolver*."""
    return web.client(
        http=HttpOptions(security=SecurityOptions(block_private_hosts=True, resolver=resolver)), base_url=base_url
    )


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


async def test_an_unknown_name_over_a_custom_transport_is_not_charged_to_the_breaker() -> None:
    """Charge nothing for a lookup the guard failed before anything was sent: no attempt reached the network."""
    web = Web()
    breakers = BreakerRegistry(BreakerPolicy(failure_threshold=1))
    client = Client(
        base_url=BASE,
        policy=CallPolicy(rate=Rate.per_second(1000), retry=None, breaker=breakers),
        http=HttpOptions(
            transport=httpx2.MockTransport(web), security=SecurityOptions(block_private_hosts=True, resolver=_Names())
        ),
    )
    async with client:
        for _ in range(2):
            with pytest.raises(TransportError, match=r"could not resolve api\.example"):
                await client.get("x", expect=RESPONSE)

    assert web.sent == []
    assert [snapshot.failures for snapshot in breaker_snapshots(breakers).values()] == [0]


async def test_the_curl_transport_is_vetted_before_it_sends() -> None:
    """Refuse a loopback hop over the curl transport before curl connects."""
    async with serving() as server:
        options = HttpOptions(
            transport=AsyncCurlTransport(impersonate="chrome"), security=SecurityOptions(block_private_hosts=True)
        )
        async with Client(base_url=server.url(), policy=CallPolicy(rate=Rate.per_second(1000)), http=options) as client:
            with pytest.raises(ForbiddenDestinationError):
                await client.get("x", expect=RESPONSE)

    assert server.connections == 0


@pytest.mark.parametrize(
    ("options", "message"),
    [
        (lambda: HttpOptions(security=SecurityOptions(resolver=_Names())), "needs block_private_hosts=True"),
        (
            lambda: HttpOptions(proxy="http://proxy.example:3128", security=SecurityOptions(block_private_hosts=True)),
            "proxy",
        ),
    ],
    ids=["resolver-without-guard", "guard-with-proxy"],
)
def test_a_guard_setting_that_cannot_work_is_refused(options: object, message: str) -> None:
    """Refuse a resolver that nothing would use, and the guard behind a proxy, which resolves past it."""
    assert callable(options)
    with pytest.raises(ValueError, match=message):
        options()


def _admitting(*allowed: str) -> AddressCheck:
    """Return a check that admits *allowed* and defers every other address to the default check."""

    def check(address: str) -> str | None:
        return None if address in allowed else forbidden_reason(address)

    return check


def _checked(base_url: str, check: AddressCheck, transport: httpx2.AsyncBaseTransport | None = None) -> Client:
    """Build a client on *base_url* whose SSRF guard judges addresses with *check*, over *transport* if given."""
    security = SecurityOptions(block_private_hosts=True, check=check)
    return Client(
        base_url=base_url,
        policy=CallPolicy(rate=Rate.per_second(1000)),
        http=HttpOptions(transport=transport, security=security),
    )


async def test_a_check_widens_the_guard_over_the_default_transport() -> None:
    """Reach a loopback server the default check refuses, once ``check=`` admits its address."""
    async with serving() as server, _checked(server.url(), _admitting("127.0.0.1")) as client:
        resp = await client.get("x", expect=RESPONSE)

    assert resp.text == "ok"
    assert server.connections == 1


async def test_a_check_widens_the_guard_over_the_curl_transport() -> None:
    """Vet the curl transport's own lookups with the client's ``check=`` too."""
    curl = AsyncCurlTransport(impersonate="chrome")
    async with serving() as server, _checked(server.url(), _admitting("127.0.0.1"), curl) as client:
        resp = await client.get("x", expect=RESPONSE)

    assert resp.text == "ok"


async def test_a_check_judges_every_hop() -> None:
    """Judge a redirect's next hop with ``check=`` as well: one private address is admitted, another refused."""
    web = Web(
        {f"{BASE}/start": "https://internal.example/next", "https://internal.example/next": "https://db.example/"}
    )
    names = _Names(api_example=_PUBLIC, internal_example="10.1.2.3", db_example="10.9.9.9")
    security = SecurityOptions(block_private_hosts=True, resolver=names, check=_admitting("10.1.2.3"))
    async with web.client(http=HttpOptions(security=security), base_url=BASE) as client:
        with pytest.raises(ForbiddenDestinationError, match=r"db\.example: it resolves to 10\.9\.9\.9"):
            await client.get("start", expect=RESPONSE)

    assert [str(request.url) for request in web.sent] == [f"{BASE}/start", "https://internal.example/next"]


@dataclasses.dataclass
class _Unhashable(httpx2.AsyncBaseTransport):
    """A transport a dataclass makes unhashable, as ``@dataclass`` does by its ``__eq__``."""

    status: int = 200

    @override
    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(self.status, request=request)


class _Slotted(httpx2.AsyncBaseTransport):
    """A transport with slots, and so no instance dictionary."""

    __slots__ = ()

    @override
    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, request=request)


@pytest.mark.parametrize("transport", [_Unhashable(), _Slotted()], ids=["dataclass", "slots"])
async def test_any_custom_transport_is_accepted(transport: httpx2.AsyncBaseTransport) -> None:
    """Build a client on a transport of any shape, guarded or not: only the curl transport pins itself."""
    for security in (
        SecurityOptions(),
        SecurityOptions(block_private_hosts=True, resolver=_Names(api_example=_PUBLIC)),
    ):
        async with Client(base_url=BASE, http=HttpOptions(transport=transport, security=security)) as client:
            await client.get("x", expect=RESPONSE)


def test_a_check_needs_the_guard() -> None:
    """Refuse a check of your own nothing would use, and one that is not a function, as ``resolver=`` is refused."""
    with pytest.raises(
        ValueError, match=r"^check= is used only by the SSRF guard, so it needs block_private_hosts=True$"
    ):
        SecurityOptions(check=_admitting("127.0.0.1"))
    with pytest.raises(TypeError, match=r"^check= takes an address check"):
        SecurityOptions(block_private_hosts=True, check=cast("Any", "loopback"))


def test_the_default_check_is_a_value() -> None:
    """Hold ``forbidden_reason`` as the default, which needs no guard, and refuse ``None``, naming the default."""
    assert SecurityOptions().check is forbidden_reason
    assert SecurityOptions(check=forbidden_reason).check is forbidden_reason
    with pytest.raises(TypeError, match=r"^check= takes an address check, not None: leave it out for forbidden_reason"):
        SecurityOptions(block_private_hosts=True, check=cast("Any", None))


def test_the_guard_is_configured_only_through_security_options() -> None:
    """Keep the guard's machinery private: a check and a resolver are what a client of it writes."""
    assert set(headgate.security.__all__) == {
        "AddressCheck",
        "CredentialCarrier",
        "CredentialStamper",
        "Resolver",
        "forbidden_reason",
    }
    assert not hasattr(AsyncCurlTransport, "bind_guard")


class _Recording(_Names):
    """A resolver over a fixed table that records each ``(host, port)`` it is asked for."""

    def __init__(self, **table: str) -> None:
        """Resolve each name in *table*, recording every lookup."""
        super().__init__(**table)
        self.asked: list[tuple[str, int]] = []

    @override
    async def __call__(self, host: str, _port: int) -> Sequence[str]:
        """Record the lookup, then resolve *host*."""
        self.asked.append((host, _port))
        return await super().__call__(host, _port)


#: A port no scheme defaults to, so the resolver can only have learned it from the URL.
_ODD_PORT: Final = 8443


async def test_the_resolver_is_asked_for_each_hops_host_and_port_over_a_custom_transport() -> None:
    """Ask the resolver for each hop's host and port: one the URL names, a scheme's default, 0 for an unknown scheme."""
    first = f"http://api.example:{_ODD_PORT}/start"
    web = Web({first: "http://api.example/next", "http://api.example/next": "custom://api.example/last"})
    names = _Recording(api_example=_PUBLIC)
    async with _web_client(web, names, base_url=f"http://api.example:{_ODD_PORT}") as client:
        await client.get("start", expect=RESPONSE)

    assert names.asked == [("api.example", _ODD_PORT), ("api.example", 80), ("api.example", 0)]


async def test_a_failed_lookup_is_chained_to_a_connect_error_naming_the_hop() -> None:
    """Chain the failed lookup's ``TransportError`` to the ``httpx2.ConnectError`` for the hop it failed on."""
    async with _web_client(Web(), _Names()) as client:
        with pytest.raises(TransportError) as exc:
            await client.get("x", expect=RESPONSE)

    cause = exc.value.__cause__
    assert isinstance(cause, httpx2.ConnectError)
    assert str(cause.request.url) == f"{BASE}/x"


class _Failover:
    """A middleware that sends each attempt to a replica host instead, as a failover would."""

    credential_headers = frozenset[str]()
    credential_params = frozenset[str]()

    def __init__(self, host: str) -> None:
        """Send to *host*."""
        self._host = host

    async def __call__(self, request: httpx2.Request, call_next: Next) -> httpx2.Response:
        """Change the request's host, then send."""
        request.url = request.url.copy_with(host=self._host)
        return await call_next(request)


@pytest.mark.parametrize("replica", ["10.0.0.9", "replica.example"], ids=["literal", "name"])
async def test_a_host_a_middleware_chose_is_vetted_over_a_custom_transport(replica: str) -> None:
    """Vet the host a middleware sends to again below it, which the request hook never saw, before it is sent."""
    web = Web()
    names = _Names(api_example=_PUBLIC, replica_example="10.0.0.9")
    client = web.client(
        http=HttpOptions(security=SecurityOptions(block_private_hosts=True, resolver=names)),
        middleware=(_Failover(replica),),
    )
    async with client:
        with pytest.raises(ForbiddenDestinationError, match=r"refused to connect to (10\.0\.0\.9|replica\.example)"):
            await client.get("x", expect=RESPONSE)

    assert web.sent == []


async def test_a_public_host_a_middleware_chose_is_sent_over_a_custom_transport() -> None:
    """Send to the host a middleware chose once the guard admits it."""
    web = Web()
    names = _Names(api_example=_PUBLIC, replica_example=_PUBLIC)
    client = web.client(
        http=HttpOptions(security=SecurityOptions(block_private_hosts=True, resolver=names)),
        middleware=(_Failover("replica.example"),),
    )
    async with client:
        await client.get("x", expect=RESPONSE)

    assert [request.url.host for request in web.sent] == ["replica.example"]


class _NewPath:
    """A middleware that sends each attempt to another path on the origin it came with."""

    credential_headers = frozenset[str]()
    credential_params = frozenset[str]()

    async def __call__(self, request: httpx2.Request, call_next: Next) -> httpx2.Response:
        """Change the request's path, then send."""
        request.url = request.url.copy_with(path="/v2/x")
        return await call_next(request)


class _Rebuilt:
    """A middleware that sends a request of its own, built afresh without the attempt's extensions, to the same URL."""

    credential_headers = frozenset[str]()
    credential_params = frozenset[str]()

    async def __call__(self, request: httpx2.Request, call_next: Next) -> httpx2.Response:
        """Send a new request for the same URL."""
        return await call_next(httpx2.Request(request.method, request.url))


@pytest.mark.parametrize(
    ("middleware", "asked"),
    [
        (_NewPath(), [("api.example", 443)]),
        (_Failover("replica.example"), [("api.example", 443), ("replica.example", 443)]),
        (_Rebuilt(), [("api.example", 443), ("api.example", 443)]),
    ],
    ids=["same-origin", "moved", "rebuilt"],
)
async def test_a_hop_is_vetted_again_below_the_middleware_only_for_an_origin_it_moved_to(
    middleware: Middleware, asked: list[tuple[str, int]]
) -> None:
    """Look a hop's host up once, and again below the middleware only for an origin it moved the request to."""
    web = Web()
    names = _Recording(api_example=_PUBLIC, replica_example=_PUBLIC)
    client = web.client(
        http=HttpOptions(security=SecurityOptions(block_private_hosts=True, resolver=names)), middleware=(middleware,)
    )
    async with client:
        await client.get("x", expect=RESPONSE)

    assert names.asked == asked


class _ToReplica:
    """A failover middleware that moves each attempt, in place, to ``replica.example``: a retry comes back moved."""

    credential_headers = frozenset[str]()
    credential_params = frozenset[str]()

    def __init__(self, on_second: Callable[[], None] = lambda: None) -> None:
        """Call *on_second* when the second attempt comes in."""
        self.calls = 0
        self._on_second = on_second

    async def __call__(self, request: httpx2.Request, call_next: Next) -> httpx2.Response:
        """Move the request to the replica, then send."""
        self.calls += 1
        if self.calls == len(("first", "second")):
            self._on_second()
        request.url = request.url.copy_with(host="replica.example")
        return await call_next(request)


def _guarded_over(web: Web, names: _Names, failover: _ToReplica, retry: RetryPolicy) -> Client:
    """Build a client over *web*, guarded with *names*, through *failover*, retrying by *retry*."""
    return Client(
        base_url=BASE,
        policy=CallPolicy(rate=Rate.per_second(1000), retry=retry),
        http=HttpOptions(
            transport=httpx2.MockTransport(web), security=SecurityOptions(block_private_hosts=True, resolver=names)
        ),
        middleware=(failover,),
    )


async def test_a_retry_of_an_attempt_a_middleware_moved_is_vetted_where_it_goes() -> None:
    """Vet a retry the middleware moved before the guard admitted the move, which comes back already moved.

    The replica does not resolve on the first attempt, a failure that is retried, and resolves to a private address by
    the second: the retry, which enters the middleware on the replica already, is vetted there and refused.
    """
    web = Web()
    names = _Names(api_example=_PUBLIC)
    failover = _ToReplica(on_second=lambda: names.table.update({"replica.example": "10.0.0.9"}))
    client = _guarded_over(web, names, failover, RetryPolicy(attempts=2))
    async with client:
        with pytest.raises(ForbiddenDestinationError, match=r"refused to connect to replica\.example"):
            await client.get("x", expect=RESPONSE)

    assert failover.calls == len(("first", "second"))
    assert web.sent == []


class _SlowNames(_Names):
    """A resolver that takes a while for the replica, so a hedge starts before its first lookup ends."""

    @override
    async def __call__(self, host: str, _port: int) -> Sequence[str]:
        """Answer the replica a fifth of a second late."""
        if host == "replica.example":
            await asyncio.sleep(0.2)
        return await super().__call__(host, _port)


async def test_a_hedge_of_an_attempt_a_middleware_moved_is_vetted_where_it_goes() -> None:
    """Vet a hedge copied from an attempt the middleware moved while the guard was still looking the move up."""
    web = Web()
    client = _guarded_over(
        web,
        _SlowNames(api_example=_PUBLIC, replica_example="10.0.0.9"),
        _ToReplica(),
        RetryPolicy(attempts=2, hedge=Hedge(after=0.01)),
    )
    async with client:
        with pytest.raises(ForbiddenDestinationError, match=r"refused to connect to replica\.example"):
            await client.get("x", expect=RESPONSE)

    assert web.sent == []


async def test_a_host_a_middleware_chose_is_vetted_inside_a_fake_network() -> None:
    """Vet the middleware's host through the fake network's names, so the test sees the refusal production would."""
    fake = FakeTransport().reply("GET", "/x", 200)
    with fake_network(fake) as net:
        net.resolve("replica.example", "10.0.0.7")
        client = Client(
            base_url=BASE,
            policy=CallPolicy(rate=Rate.per_second(1000)),
            http=HttpOptions(security=SecurityOptions(block_private_hosts=True)),
            middleware=(_Failover("replica.example"),),
        )

    with pytest.raises(ForbiddenDestinationError, match=r"resolves to 10\.0\.0\.7, which is a private address"):
        await client.get("x", expect=RESPONSE)
    assert fake.calls == ()
