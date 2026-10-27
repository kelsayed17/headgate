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

#: The redirects httpx2 turns into a body-less GET for a POST (RFC 9110 section 15.4.2 and 15.4.3 allow it).
_POST_BECOMES_GET: Final = frozenset({httpx2.codes.MOVED_PERMANENTLY, httpx2.codes.FOUND})


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

    It does for every redirect but a 303, which becomes a GET, and a 301 or 302 answering a POST, which httpx2 turns
    into a GET as browsers do.
    """
    status = response.status_code
    if status == httpx2.codes.SEE_OTHER:
        return False
    return not (status in _POST_BECOMES_GET and response.request.method == "POST")


async def refuse_unreplayable_redirect(response: httpx2.Response) -> None:
    """Stop httpx2 following a redirect that would send a one-shot request body again (a response hook).

    A ``content=`` given as an async iterable is consumed by the first send (:data:`~headgate.retry.REPLAYABLE_BODY`),
    so following a 307 or 308 would fail inside httpx2 with ``StreamConsumed``. The ``Location`` is removed instead, as
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
