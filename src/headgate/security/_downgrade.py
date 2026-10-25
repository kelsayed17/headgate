# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""A redirect never takes a request from ``https`` down to plain ``http``.

httpx2 follows any redirect a response names. One from an ``https`` URL to an ``http`` one would send the next request,
its body included, in cleartext, so :func:`refuse_https_downgrade`, a response hook, stops it before httpx2 builds the
next hop: the 3xx becomes the final response and is raised through the status ladder as the
:class:`~headgate.UpstreamStatusError` it is, with a note naming the refused target.
"""

from __future__ import annotations

import httpx2

from headgate._core import CallContext
from headgate._failures import loggable_url

__all__ = ["refuse_https_downgrade"]


async def refuse_https_downgrade(response: httpx2.Response) -> None:
    """Stop httpx2 following a redirect from ``https`` to ``http`` (a response hook).

    httpx2 follows a response only while it has a ``Location``, so the header is removed: the response then reaches
    the call as its final answer. A redirect the call does not follow is its final answer already and is left whole,
    so a call that declares the 3xx still reads its ``Location``. A ``Location`` httpx2 cannot parse is left for httpx2
    to refuse.
    """
    if not response.has_redirect_location or response.request.url.scheme != "https":
        return
    context = CallContext.find(response.request)
    if context is not None and not context.follow_redirects:
        return
    try:
        target = httpx2.URL(response.headers["Location"])
    except httpx2.InvalidURL:
        return
    if target.scheme != "http":
        return
    del response.headers["Location"]
    if context is not None:
        context.add_note(f"refused to follow a redirect from https to {loggable_url(str(target))}")
