# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""Pagination: the strategies a paginated endpoint is followed with, and the lazy result of following it.

``client.paginate(url, expect=..., pages=Cursor(...))`` returns a :class:`Paginated`, iterated with ``async for``. The
strategies (:class:`Cursor`, :class:`Offset`, :class:`LinkHeader`, :class:`NextUrl`) implement :class:`Pager`, which
a custom scheme implements too. :func:`parse_link_header` is the RFC 8288 parser :class:`LinkHeader` reads with.
"""

from headgate.pagination._link import Link, parse_link_header
from headgate.pagination._pager import Cursor, LinkHeader, NextUrl, Offset, Pager, PageRequest
from headgate.pagination._paginated import PageLimit, Paginated

__all__ = [
    "Cursor",
    "Link",
    "LinkHeader",
    "NextUrl",
    "Offset",
    "PageLimit",
    "PageRequest",
    "Pager",
    "Paginated",
    "parse_link_header",
]
