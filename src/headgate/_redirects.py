# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""What a followed redirect may keep: its body is dropped (a response hook every client installs).

:func:`drop_followed_redirect_body` is installed by :func:`headgate._stack.build_http_client`; the call's
:class:`~headgate._core.CallContext` tells it whether this request's redirects are being followed. Where a redirect may
take a request, and what it may carry there, is :mod:`headgate.security`'s.
"""

from __future__ import annotations

import httpx2

from headgate._core import CallContext

__all__ = ["drop_followed_redirect_body"]


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
