# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""What a followed redirect may keep and re-send: two response hooks every client installs.

* :func:`drop_followed_redirect_body`: the body of a redirect httpx2 follows is dropped unread.
* :func:`refuse_unreplayable_redirect`: a redirect that would send a one-shot request body a second time is not
  followed; its 3xx is the call's final response.

Both are installed by :func:`headgate._stack.build_http_client`; the call's :class:`~headgate._core.CallContext` tells
them whether this request's redirects are being followed. Where a redirect may take a request, and what credentials it
may carry there, is :mod:`headgate.security`'s.
"""

from __future__ import annotations

from typing import Final

import httpx2

from headgate._core import CallContext
from headgate.retry._policy import REPLAYABLE_BODY

__all__ = ["drop_followed_redirect_body", "refuse_unreplayable_redirect"]

#: The methods httpx2 keeps on a 302; it turns any other into a GET, as browsers do (QUERY per RFC 10008 section 2.5).
_KEPT_BY_302: Final = frozenset({"HEAD", "QUERY"})


async def drop_followed_redirect_body(response: httpx2.Response) -> None:
    """Discard the body of a redirect httpx2 is about to follow (a response hook).

    httpx2 reads a followed redirect's body in full inside its own redirect loop, before the next hop, and so below
    :func:`~headgate._body.read_capped`, which only ever sees the final response. A 302 whose body decompressed far
    past ``max_response_bytes`` would otherwise be held in memory, kept alive in the response history. Nothing reads a
    followed redirect's body, so it is replaced with an empty stream rather than capped. A redirect that is not followed
    is the final response, and keeps its body for ``read_capped``. httpx2 hands the call's extensions, and so its
    context, to every hop.

    An ``AsyncClient`` always hands its hooks an async stream; the ``isinstance`` check only narrows the type.
    """
    stream = response.stream
    context = CallContext.find(response.request)
    if (
        response.has_redirect_location
        and context is not None
        and context.follow_redirects
        and isinstance(stream, httpx2.AsyncByteStream)
    ):
        await stream.aclose()
        response.stream = httpx2.ByteStream(b"")


def _resends_body(response: httpx2.Response) -> bool:
    """Return whether httpx2 follows the redirect *response* by sending its request's body again.

    It does unless it turns the request into a GET, which carries no body: a 303 answering anything but a HEAD (RFC
    9110 section 15.4.4), a 302 answering anything but a HEAD or a QUERY, and a 301 answering a POST. A request that
    already was a GET keeps its method, and so its body. This is ``httpx2.AsyncClient._redirect_method``'s rule.
    """
    return _redirect_method(response) == response.request.method


def _redirect_method(response: httpx2.Response) -> str:
    """Return the method httpx2 follows the redirect *response* with (see :func:`_resends_body`)."""
    method = response.request.method
    status = response.status_code
    if status == httpx2.codes.SEE_OTHER and method != "HEAD":
        return "GET"
    if status == httpx2.codes.FOUND and method not in _KEPT_BY_302:
        return "GET"
    if status == httpx2.codes.MOVED_PERMANENTLY and method == "POST":
        return "GET"
    return method


async def refuse_unreplayable_redirect(response: httpx2.Response) -> None:
    """Stop httpx2 following a redirect that would send a one-shot request body again (a response hook).

    A ``content=`` given as an async iterable is consumed by the first send (:data:`~headgate.retry.REPLAYABLE_BODY`),
    so following a redirect that keeps the method (a 307 or 308, or a 301 answering anything but a POST) would fail
    inside httpx2 with ``StreamConsumed``. The ``Location`` is removed instead, as
    :func:`~headgate.security.refuse_https_downgrade` does: the 3xx reaches the call as its final answer, raised through
    the status ladder unless the call declares it, with a note saying why. A body given as a factory is built afresh for
    the next hop, so its redirects are followed.
    """
    if not response.has_redirect_location:
        return
    context = CallContext.find(response.request)
    if context is None or not context.follow_redirects or context.get(REPLAYABLE_BODY) is not False:
        return
    if not _resends_body(response):
        return
    del response.headers["Location"]
    context.add_note(
        f"did not follow the {response.status_code} redirect: it asks for the request body again, and the body was a "
        "one-shot stream (pass content= a factory to make it replayable)"
    )
