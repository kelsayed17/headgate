# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""A network transport for tests: a route table of canned replies, and a log of every request it was sent."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Self, override

import httpx2

from headgate._url import sanitize_url

__all__ = ["FakeTransport", "Reply", "UnmatchedRequestError"]

type Reply = httpx2.Response | int | Callable[[httpx2.Request], httpx2.Response | Awaitable[httpx2.Response]]
"""What a route answers with: a response, a bare status, or a function of the request (sync or async)."""


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


class FakeTransport(httpx2.AsyncBaseTransport):
    """Answer requests from a route table keyed by method and path, and record every request in :attr:`calls`.

    Give it to a client as ``HttpOptions(transport=FakeTransport().reply(...))``::

        fake = FakeTransport()
        fake.reply("GET", "/v1/series/GDP", httpx2.Response(503), httpx2.Response(200, json={"id": "GDP"}))

    A route's replies are served in order to the requests that match it, the last one repeating, so a retry sequence
    is one route. A path is matched exactly against the request's path as sent: percent-encoded, as a t-string URL
    escapes it, and with the client's base path (``/v1/series/GDP%2Freal``). A method is matched case-insensitively.
    A request with no route raises :class:`UnmatchedRequestError`, naming the request and the routes there are.

    A reply is an ``httpx2.Response`` built with ``content=``, ``json=`` or ``text=`` (copied for each request, so it
    can be served many times; a response over a one-shot async stream cannot), a status for an empty response, or a
    function of the request returning a response, possibly awaitable, for a reply that depends on what was sent or
    that raises an ``httpx2`` error to simulate a network failure. Every request's body is read before it is logged
    and answered.
    """

    def __init__(self) -> None:
        """Start with no routes and an empty log."""
        self._routes: dict[tuple[str, str], _Route] = {}
        self._calls: list[httpx2.Request] = []
        self.closed = False

    def reply(self, method: str, path: str, *replies: Reply) -> Self:
        """Answer *method* requests to *path* with *replies*, replacing any route there; return the transport."""
        self._routes[method.upper(), path] = _Route(replies)
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
        await request.aread()
        self._calls.append(request)
        route = self._routes.get((request.method, _path(request)))
        if route is None:
            known = ", ".join(f"{method} {path}" for method, path in self._routes) or "none"
            msg = f"no route for {request.method} {sanitize_url(str(request.url))} (routes: {known})"
            raise UnmatchedRequestError(msg)
        reply = route.next_reply()
        if isinstance(reply, int):
            return httpx2.Response(reply, request=request)
        if isinstance(reply, httpx2.Response):
            # A response built with ``content=`` holds an in-memory stream, which can be iterated again and again.
            return httpx2.Response(reply.status_code, headers=reply.headers, stream=reply.stream, request=request)
        answer = reply(request)
        if isinstance(answer, httpx2.Response):
            return answer
        return await answer

    @override
    async def aclose(self) -> None:
        """Record that the client closed the transport."""
        self.closed = True
