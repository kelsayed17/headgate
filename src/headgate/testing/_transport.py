# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""A network transport for tests: a route table of canned replies, and a log of every request it was sent."""

import ipaddress
from collections.abc import Awaitable, Callable, Generator
from contextlib import contextmanager
from typing import Final, Self, cast, final, override

import httpx2

from headgate._client import Client
from headgate._core import check_kind
from headgate._stack import NETWORK_OVERRIDE, NetworkOverride
from headgate._url import sanitize_url
from headgate.testing._faults import Respond, response_parts

__all__ = ["FakeNetwork", "FakeTransport", "Reply", "UnmatchedRequestError", "fake_network"]

#: The address every name resolves to inside a fake network, unless the test resolved it: globally routable, so the
#: SSRF guard admits it, and never connected to, since the fake network answers every request.
FAKE_ADDRESS: Final = "1.2.3.4"

#: What ``localhost`` and every name under it resolve to inside a fake network, as RFC 6761 has resolvers answer them:
#: loopback, which the SSRF guard refuses as it does in production.
LOOPBACK_ADDRESSES: Final = ("127.0.0.1", "::1")

#: What :meth:`FakeTransport.resolve` takes as an address.
_ADDRESSES_WANTED: Final = "IP addresses as text, such as '10.0.0.1'"

type Reply = Respond | httpx2.Response | int | Callable[[httpx2.Request], httpx2.Response | Awaitable[httpx2.Response]]
"""What a route answers with: a :class:`Respond`, a response, a bare status, or a function of the request."""


class UnmatchedRequestError(AssertionError):
    """A request reached a :class:`FakeTransport` that has no route for its method and path.

    An ``AssertionError``, not an ``httpx2`` error, so the client neither retries it nor maps it onto a
    ``ClientError``: it reaches the test as the failure it is.
    """


def _path(request: httpx2.Request) -> str:
    """Return *request*'s path as it goes on the wire: percent-encoded, without the query."""
    return request.url.raw_path.partition(b"?")[0].decode("ascii")


class _Route:
    """One route's replies, served in order, the last repeating."""

    __slots__ = ("_replies", "served")

    def __init__(self, replies: tuple[Reply, ...]) -> None:
        """Serve *replies*; a lone empty 200 when there are none."""
        self._replies = replies or (200,)
        self.served = 0

    def next_reply(self) -> Reply:
        """Return the reply for the next request on this route."""
        reply = self._replies[min(self.served, len(self._replies) - 1)]
        self.served += 1
        return reply


async def _snapshot(request: httpx2.Request) -> httpx2.Request:
    """Return a copy of *request* as it is sent now: its headers, and its body read from its stream on this send."""
    body = b"".join([chunk async for chunk in cast("httpx2.AsyncByteStream", request.stream)])
    sent = httpx2.Request(request.method, request.url, content=body, extensions=dict(request.extensions))
    sent.headers = request.headers.copy()  # as sent: `content=` would add a `Content-Length` a chunked body lacks
    return sent


class FakeTransport(httpx2.AsyncBaseTransport):
    """Answer requests from a route table keyed by method and path, and record every request in :attr:`calls`.

    Give it to a client as ``HttpOptions(transport=FakeTransport().reply(...))``::

        fake = FakeTransport()
        fake.reply("GET", "/v1/series/GDP", Respond(503, retry_after=2.0), Respond(json={"id": "GDP"}))

    A route's replies are served in order to the requests that match it, the last one repeating, so a retry sequence
    is one route. A path is matched exactly against the request's path as sent: percent-encoded, as a t-string URL
    escapes it, and with the client's base path (``/v1/series/GDP%2Freal``). A method is matched case-insensitively.
    A request with no route raises :class:`UnmatchedRequestError`, naming the request and the routes there are.

    A reply is a :class:`~headgate.testing.Respond`, the step :class:`~headgate.testing.FaultyTransport` answers with
    too; an ``httpx2.Response`` built with ``content=``, ``json=`` or ``text=`` (copied for each request, so it can be
    served many times; a response over a one-shot async stream cannot); a status for an empty response; or a
    function of the request returning a response, possibly awaitable, for a reply that depends on what was sent or
    that raises an ``httpx2`` error to simulate a network failure.

    Every send is logged as a snapshot of the request as it went out: its headers then, and its body read from the
    request's stream on that send, as a network transport reads it. A request changed between attempts therefore
    shows each attempt's own headers, and a one-shot body sent a second time raises ``httpx2.StreamConsumed`` (or the
    retry layer's refusal) instead of replaying the first read. A reply function is given the snapshot.
    """

    def __init__(self) -> None:
        """Start with no routes, no names resolved and an empty log."""
        self._routes: dict[tuple[str, str], _Route] = {}
        self._answers: dict[str, tuple[str, ...]] = {}
        self._calls: list[httpx2.Request] = []
        self.closed = False

    def reply(self, method: str, path: str, *replies: Reply) -> Self:
        """Answer *method* requests to *path* with *replies*, replacing any route there; return the transport."""
        self._routes[method.upper(), path] = _Route(replies)
        return self

    def resolve(self, host: str, *addresses: str) -> Self:
        """Resolve *host* to *addresses* inside :func:`fake_network`, for a client's SSRF guard; return the transport.

        Every other name resolves to one public address, so a guarded client needs no DNS, but ``localhost`` and every
        name under it, which resolve to loopback (RFC 6761). A private address among *addresses* is refused by the
        guard, as on the real network, and no addresses at all is a name that does not resolve. The host is matched
        case-insensitively.

        :raises TypeError: for a *host* or an address that is not a ``str``.
        :raises ValueError: for an address that is not an IP address, naming it.
        """
        check_kind("host", host, str, "a host name, a str")
        for address in addresses:
            check_kind("addresses", address, str, _ADDRESSES_WANTED)
            try:
                ipaddress.ip_address(address)
            except ValueError:
                msg = f"resolve() takes {_ADDRESSES_WANTED} (got {address!r})"
                raise ValueError(msg) from None
        self._answers[host.lower()] = addresses
        return self

    @property
    def calls(self) -> tuple[httpx2.Request, ...]:
        """Every request the transport was sent, in order, matched or not."""
        return tuple(self._calls)

    def calls_to(self, method: str, path: str) -> tuple[httpx2.Request, ...]:
        """Return the requests sent to *method* *path*, in order."""
        wanted = (method.upper(), path)
        return tuple(request for request in self._calls if (request.method, _path(request)) == wanted)

    @override
    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        sent = await _snapshot(request)
        self._calls.append(sent)
        route = self._routes.get((sent.method, _path(sent)))
        if route is None:
            known = ", ".join(f"{method} {path}" for method, path in self._routes) or "none"
            msg = f"no route for {sent.method} {sanitize_url(str(sent.url))} (routes: {known})"
            raise UnmatchedRequestError(msg)
        reply = route.next_reply()
        if isinstance(reply, Respond):
            headers, body = response_parts(reply)
            return httpx2.Response(reply.status_code, headers=headers, content=body, request=request)
        if isinstance(reply, int):
            return httpx2.Response(reply, request=request)
        if isinstance(reply, httpx2.Response):
            # A response built with ``content=`` holds an in-memory stream, which can be iterated again and again.
            return httpx2.Response(reply.status_code, headers=reply.headers, stream=reply.stream, request=request)
        answer = reply(sent)
        if isinstance(answer, httpx2.Response):
            return answer
        return await answer

    @override
    async def aclose(self) -> None:
        """Record that the client closed the transport."""
        self.closed = True


@final
class FakeNetwork[T: httpx2.AsyncBaseTransport]:
    """What :func:`fake_network` yields: the transport it puts clients on, and the clients built in its block.

    ``with fake_network(fake) as net:`` then ``budget_for(net.clients[0])``, so a test reads back what an SDK built
    without reaching into the SDK's private attribute.
    """

    __slots__ = ("_override", "_transport")

    _transport: T

    def __init__(self, *args: object, **kwargs: object) -> None:
        """Refuse to build one, whatever it is given: :func:`fake_network` does, for the block it yields it to.

        :raises TypeError: always.
        """
        del args, kwargs
        msg = "FakeNetwork is yielded by fake_network(transport): write with fake_network(fake) as net"
        raise TypeError(msg)

    @property
    def transport(self) -> T:
        """The transport every client built in the block sends on."""
        return self._transport

    @property
    def clients(self) -> tuple[Client, ...]:
        """The clients whose construction finished in the block, in order; a view of one is not another."""
        return cast("tuple[Client, ...]", tuple(self._override.clients))

    @override
    def __repr__(self) -> str:
        return f"FakeNetwork({type(self._transport).__name__}, clients={len(self._override.clients)})"


def _resolver_of(transport: httpx2.AsyncBaseTransport) -> Callable[[str, int], Awaitable[tuple[str, ...]]]:
    """Return the resolver of a fake network on *transport*.

    A name a :class:`FakeTransport` was told (:meth:`FakeTransport.resolve`) gets its answer; ``localhost`` and the
    names under it get :data:`LOOPBACK_ADDRESSES`; every other name gets :data:`FAKE_ADDRESS`. Any other transport
    has no answers of its own.
    """
    # Read from the instance itself, as any transport may be given: one that is not a FakeTransport has none.
    answers = cast(
        "dict[str, tuple[str, ...]]",
        vars(transport).get("_answers", {}) if isinstance(transport, FakeTransport) else {},
    )

    async def resolve(host: str, port: int) -> tuple[str, ...]:
        del port
        name = host.lower()
        if name in answers:
            return answers[name]
        if name == "localhost" or name.endswith(".localhost"):
            return LOOPBACK_ADDRESSES
        return (FAKE_ADDRESS,)

    return resolve


@contextmanager
def fake_network[T: httpx2.AsyncBaseTransport](transport: T) -> Generator[FakeNetwork[T]]:
    """Send every client built in the block on *transport*, in place of the network, and yield a :class:`FakeNetwork`.

    It replaces even a transport the client was given itself (``HttpOptions(transport=...)``), so an SDK that fixes
    its own transport in its constructor can still be tested over a :class:`FakeTransport`. Only the network is
    replaced: the rate budget, the cache, the retries, the breaker and the SSRF guard above it are the client's own.
    The guard resolves names inside the fake network, in place of the client's resolver, so a guarded client needs no
    DNS: every name resolves to one public address, ``localhost`` and the names under it to loopback, unless
    :meth:`FakeTransport.resolve` said otherwise. It replaces an SDK's own ``SecurityOptions(resolver=)`` too, so
    test a resolver of your own directly. A client is
    read when it is built, so build the SDK inside the block; the block need not stay open while it is used. The
    requests a :class:`~headgate.CachedTokenAuth` mints with travel the client's stack, so they reach the fake too.

    ``with fake_network(fake) as net:`` gives the handle: ``net.transport`` is *transport*, and ``net.clients`` the
    clients built in the block, for :func:`~headgate.testing.budget_for`, :func:`~headgate.testing.cache_of` and
    :func:`~headgate.testing.breakers_of`.
    """
    override = NetworkOverride(transport, _resolver_of(transport))
    # Built past its constructor, which refuses everyone else: the handle is fake_network's to give.
    network = cast("FakeNetwork[T]", object.__new__(FakeNetwork))
    object.__setattr__(network, "_transport", transport)
    object.__setattr__(network, "_override", override)
    token = NETWORK_OVERRIDE.set(override)
    try:
        yield network
    finally:
        NETWORK_OVERRIDE.reset(token)
