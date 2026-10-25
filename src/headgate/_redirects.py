# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""What a redirect may do to a request: the two hooks every client installs.

:func:`drop_followed_redirect_body` is a response hook and :func:`credential_header_guard` a request hook; both are
installed by :func:`headgate._stack.build_http_client`. The call's :class:`~headgate._core.CallContext` tells the first
one whether this request's redirects are being followed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx2

from headgate._core import CallContext

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from headgate._auth import StaticHeaderAuth

__all__ = ["credential_header_guard", "drop_followed_redirect_body"]


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


def credential_header_guard(
    auth: StaticHeaderAuth, base_url: httpx2.URL
) -> Callable[[httpx2.Request], Awaitable[None]]:
    """Build a request hook withholding *auth*'s headers from any hop that leaves *base_url*'s origin.

    httpx2 strips only ``Authorization`` and ``Cookie`` when a redirect crosses origins, so a credential in any other
    header would follow the redirect wherever it pointed. httpx2 runs request hooks before every hop, redirects
    included, so the redirect is still followed (a provider may legitimately redirect to a signed file URL on another
    host) and only the credential stays behind.
    """
    names = auth.header_names
    home = base_url.origin

    async def guard(request: httpx2.Request) -> None:
        if request.url.origin != home:
            for name in names:
                request.headers.pop(name, None)

    return guard
