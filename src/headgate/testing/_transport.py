# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""A network transport for tests: a route table of canned replies, and a log of every request it was sent."""

import inspect
import ipaddress
from collections.abc import Awaitable, Callable, Generator
from contextlib import contextmanager
from typing import Final, Self, cast, final, override

import httpx2

from headgate._client import Client
from headgate._core import check_kind, check_status, origin_text, sealed
from headgate._stack import (
    NETWORK_OVERRIDE,
    OFFLINE_TRANSPORTS,
    NetworkOverride,
    UnmatchedRequestError,
    sends_on_the_network,
)
from headgate._url import sanitize_url
from headgate.testing._faults import FaultyTransport, Respond, response_parts

__all__ = ["FakeNetwork", "FakeTransport", "Reply", "UnmatchedRequestError", "fake_network"]

#: The address every name resolves to inside a fake network, unless the test resolved it: globally routable, so the
#: SSRF guard admits it, and never connected to, since the fake network answers every request.
FAKE_ADDRESS: Final = "1.2.3.4"

#: What ``localhost`` and every name under it resolve to inside a fake network, as RFC 6761 has resolvers answer them:
#: loopback, which the SSRF guard refuses as it does in production.
LOOPBACK_ADDRESSES: Final = ("127.0.0.1", "::1")

#: What :meth:`FakeNetwork.resolve` takes as an address.
_ADDRESSES_WANTED: Final = "IP addresses as text, such as '10.0.0.1'"

#: What a route's method, path and replies take, for the refusal of anything else.
_METHOD_WANTED: Final = "an HTTP method, a str such as 'GET'"
_PATH_WANTED: Final = "a path as sent, such as '/v1/items', or an absolute URL, such as 'https://auth.example/token'"
_REPLIES_WANTED: Final = "a Respond, an httpx2.Response, a status, or a function of the request"

type Reply = Respond | httpx2.Response | int | Callable[[httpx2.Request], httpx2.Response | Awaitable[httpx2.Response]]
"""What a route answers with: a :class:`Respond`, a response, a bare status, or a function of the request."""


def _path(request: httpx2.Request) -> str:
    """Return *request*'s path as it goes on the wire: percent-encoded, without the query."""
    return request.url.raw_path.partition(b"?")[0].decode("ascii")


def _route(path: str) -> tuple[str, str]:
    """Return the origin (``""`` for any) and the path a route written as *path* matches: a path, or an absolute URL.

    A path is escaped as an absolute URL's is, a space as ``%20`` and a non-ASCII character as its UTF-8 bytes, so the
    two forms agree, and each matches the path a request sends.

    :raises TypeError: for a *path* that is not a ``str``.
    :raises ValueError: for a query or a fragment, which a route never matches, a relative path, and an absolute URL
        that is malformed or neither ``http`` nor ``https``, saying how to write each.
    """
    check_kind("path", path, str, _PATH_WANTED)
    # A path as sent is percent-encoded, so a "?" or "#" in it starts a query or a fragment.
    if (cut := next((index for index, char in enumerate(path) if char in "?#"), None)) is not None:
        msg = (
            f"path= takes {_PATH_WANTED} (got {path!r}): a route matches no query or fragment, so write {path[:cut]!r}"
        )
        raise ValueError(msg)
    absolute = not path.startswith("/")
    try:
        # A path is read on a placeholder origin, so it is escaped as an absolute URL's path is.
        url = httpx2.URL(path if absolute else f"http://route{path}")
    except httpx2.InvalidURL as exc:
        msg = f"path= takes {_PATH_WANTED} (got {path!r}): it is not a URL httpx2 can read ({exc})"
        raise ValueError(msg) from None
    if not url.is_absolute_url:
        msg = (
            f"path= takes {_PATH_WANTED} (got {path!r}): start a path with '/', the client's base path included, or "
            "give an absolute URL its scheme"
        )
        raise ValueError(msg)
    if url.scheme not in {"http", "https"}:
        msg = f"path= takes {_PATH_WANTED} (got {path!r}): a client sends only http and https URLs"
        raise ValueError(msg)
    return origin_text(url.origin) if absolute else "", url.raw_path.decode("ascii")


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


@final
@sealed("answer a route with a function of the request, or wrap it in a transport of your own")
class FakeTransport(httpx2.AsyncBaseTransport):
    """Answer requests from a route table keyed by method and path or URL, and record every request in :attr:`calls`.

    Give it to a client as ``HttpOptions(transport=FakeTransport().reply(...))``::

        fake = FakeTransport()
        fake.reply("GET", "/v1/series/GDP", Respond(503, retry_after=2.0), Respond(json={"id": "GDP"}))

    A route's replies are served in order to the requests that match it, the last one repeating, so a retry sequence
    is one route. A path is matched exactly against the request's path as sent: percent-encoded, as a t-string URL
    escapes it, and with the client's base path (``/v1/series/GDP%2Freal``), on any host. A character a URL escapes is
    escaped in the route too, so ``/v1/a b`` matches ``/v1/a%20b``. A route written as an absolute ``http`` or
    ``https`` URL (``https://auth.example/oauth/token``) matches that origin's path alone, before a route of the path,
    so a token host and an API host answer apart, and an SDK that sends to the wrong host matches nothing. Neither form
    has a query or a fragment, which no route matches. A method is matched case-insensitively. A request with no route
    raises :class:`UnmatchedRequestError`, naming the request and the routes there are.

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

    #: Whether a client closed the transport: ``False`` until its ``aclose()``.
    closed: bool

    def __init__(self) -> None:
        """Start with no routes and an empty log."""
        self._routes: dict[tuple[str, str, str], _Route] = {}
        self._calls: list[httpx2.Request] = []
        self.closed = False

    def reply(self, method: str, path: str, *replies: Reply) -> Self:
        """Answer *method* requests to *path*, a path or an absolute URL, with *replies*, replacing any route there.

        Return the transport.

        :raises TypeError: for a *method* or a *path* that is not a ``str``, and a reply of none of a reply's kinds, a
            class such as ``Respond`` itself included.
        :raises ValueError: for a *path* with a query or a fragment, a relative path, an absolute URL that is malformed
            or neither ``http`` nor ``https``, and a status outside 100-599, which no response has.
        """
        check_kind("method", method, str, _METHOD_WANTED)
        origin, matched = _route(path)
        for reply in replies:
            # A class is callable, but calling it with the request builds no response.
            if isinstance(reply, bool | type) or not (
                isinstance(reply, Respond | httpx2.Response | int) or callable(reply)
            ):
                check_kind("reply(*replies)", reply, (), _REPLIES_WANTED)
            if isinstance(reply, int):
                check_status("reply(*replies)", reply, _REPLIES_WANTED)
        self._routes[method.upper(), origin, matched] = _Route(replies)
        return self

    @property
    def calls(self) -> tuple[httpx2.Request, ...]:
        """Every request the transport was sent, in order, matched or not."""
        return tuple(self._calls)

    def calls_to(self, method: str, path: str) -> tuple[httpx2.Request, ...]:
        """Return the requests sent to *method* *path*, in order: a path on any host, or an absolute URL's alone.

        :raises TypeError: for a *method* or a *path* that is not a ``str``.
        :raises ValueError: for a *path* :meth:`reply` refuses.
        """
        check_kind("method", method, str, _METHOD_WANTED)
        origin, matched = _route(path)
        wanted = method.upper()
        return tuple(
            request
            for request in self._calls
            if (request.method, _path(request)) == (wanted, matched) and origin in {"", origin_text(request.url.origin)}
        )

    @override
    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        sent = await _snapshot(request)
        self._calls.append(sent)
        path = _path(sent)
        route = self._routes.get((sent.method, origin_text(sent.url.origin), path))
        if route is None:
            route = self._routes.get((sent.method, "", path))
        if route is None:
            known = ", ".join(f"{method} {origin}{path}" for method, origin, path in self._routes) or "none"
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


# A client built outside every block on one of these reaches no network, so another test's block lets it send.
OFFLINE_TRANSPORTS.update((FakeTransport, FaultyTransport))


@final
@sealed("fake_network(transport) yields it")
class FakeNetwork[T: httpx2.AsyncBaseTransport]:
    """What :func:`fake_network` yields: the transport it puts clients on, and the clients built in its block.

    ``with fake_network(fake) as net:`` then ``budget_for(net.clients[0])``, so a test reads back what an SDK built
    without reaching into the SDK's private attribute. Names are the network's too: :meth:`resolve` gives a host the
    answer the SSRF guard of every client built in the block gets, whatever transport the block was given.
    """

    __slots__ = ("_answers", "_override", "_transport")

    #: What ``inspect`` and ``help()`` show it takes: nothing, since ``fake_network`` builds it.
    __signature__ = inspect.Signature()

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

    def resolve(self, host: str, *addresses: str) -> Self:
        """Resolve *host* to *addresses* for the SSRF guard of every client built in the block; return the network.

        Every other name resolves to one public address, so a guarded client needs no DNS, but ``localhost`` and every
        name under it, which resolve to loopback (RFC 6761). A private address among *addresses* is refused by the
        guard, as on the real network, and no addresses at all is a name that does not resolve. The host is matched as
        a name, in any of its spellings: in any case, a Unicode name as its IDNA (ASCII) form, and with or without one
        final dot. The answer holds from the next lookup on, in the block or after it.

        :raises TypeError: for a *host* or an address that is not a ``str``.
        :raises ValueError: for a *host* no URL can hold (IDNA refuses it), and an address that is not an IP address,
            naming it.
        """
        check_kind("host", host, str, "a host name, a str")
        for address in addresses:
            check_kind("resolve(*addresses)", address, str, _ADDRESSES_WANTED)
            try:
                ipaddress.ip_address(address)
            except ValueError:
                msg = f"resolve(*addresses) takes {_ADDRESSES_WANTED} (got {address!r})"
                raise ValueError(msg) from None
        try:
            name = _name(host)
        except httpx2.InvalidURL as exc:
            msg = f"host= takes a host name, a str (got {host!r}): {exc}"
            raise ValueError(msg) from None
        self._answers[name] = addresses
        return self

    @override
    def __repr__(self) -> str:
        return f"FakeNetwork({type(self._transport).__name__}, clients={len(self._override.clients)})"


def _name(host: str) -> str:
    """Return *host* as one spelling of its name: lower case, a Unicode name as IDNA's ASCII, one final dot dropped.

    It is the host as httpx2 puts it in a URL, which the SSRF guard looks up, so the spellings agree on both sides.

    :raises httpx2.InvalidURL: for a host IDNA refuses.
    """
    return httpx2.URL(scheme="http", host=host).raw_host.decode("ascii").removesuffix(".")


def _resolver(answers: dict[str, tuple[str, ...]]) -> Callable[[str, int], Awaitable[tuple[str, ...]]]:
    """Return the resolver of a fake network that resolves the names in *answers*, read at each lookup, as they say.

    ``localhost`` and the names under it get :data:`LOOPBACK_ADDRESSES`; every other name gets :data:`FAKE_ADDRESS`.
    A host is looked up as :func:`_name` spells it, as :meth:`FakeNetwork.resolve` stored it.
    """

    async def resolve(host: str, port: int) -> tuple[str, ...]:
        del port
        name = _name(host)
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
    its own transport in its constructor can still be tested over a :class:`FakeTransport`. *transport* is a fake,
    which answers in place of the network: one that sends on it (``httpx2.AsyncHTTPTransport``,
    ``headgate.curl.AsyncCurlTransport``) is refused. Only the network is
    replaced: the rate budget, the cache, the retries, the breaker and the SSRF guard above it are the client's own.
    The guard resolves names inside the fake network, in place of the client's resolver, so a guarded client needs no
    DNS: every name resolves to one public address, ``localhost`` and the names under it to loopback, unless
    :meth:`FakeNetwork.resolve` said otherwise. It replaces an SDK's own ``SecurityOptions(resolver=)`` too, so
    test a resolver of your own directly. A client is
    read when it is built, so build the SDK inside the block; the block need not stay open while it is used. A client
    built outside every block, on a network other than *transport*, refuses each call made inside one with
    :class:`UnmatchedRequestError`, before anything is sent, so a test's credentials never reach the real network; one
    on a :class:`FakeTransport` or :class:`~headgate.testing.FaultyTransport` of its own reaches no network, and sends
    on it. The block is the calling task's context (a ``ContextVar``): a task started before the block was entered,
    and another thread, run outside it, so a call they make is neither refused nor sent on *transport*. The
    requests a :class:`~headgate.CachedTokenAuth` mints with travel the client's stack, so they reach the fake too.

    ``with fake_network(fake) as net:`` gives the handle: ``net.transport`` is *transport*, ``net.clients`` the
    clients built in the block, for :func:`~headgate.testing.budget_for`, :func:`~headgate.testing.cache_of` and
    :func:`~headgate.testing.breakers_of`, and ``net.resolve(host, *addresses)`` what a host resolves to.

    :raises TypeError: for a *transport* that is not an ``httpx2.AsyncBaseTransport``, or is one that sends on the
        network, when the block is entered.
    """
    check_kind(
        "transport", transport, httpx2.AsyncBaseTransport, "an httpx2.AsyncBaseTransport, such as FakeTransport()"
    )
    # The guard would vet through the block's names while a real transport looks the host up itself.
    if sends_on_the_network(transport):
        msg = (
            "transport= takes a fake, which answers in place of the network, such as FakeTransport() (got "
            f"{type(transport).__name__}, which sends on it): to reach a real server, build the client outside a block"
        )
        raise TypeError(msg)
    answers: dict[str, tuple[str, ...]] = {}
    override = NetworkOverride(transport, _resolver(answers))
    # Built past its constructor, which refuses everyone else: the handle is fake_network's to give.
    network = cast("FakeNetwork[T]", object.__new__(FakeNetwork))
    object.__setattr__(network, "_transport", transport)
    object.__setattr__(network, "_override", override)
    object.__setattr__(network, "_answers", answers)
    token = NETWORK_OVERRIDE.set(override)
    try:
        yield network
    finally:
        NETWORK_OVERRIDE.reset(token)
