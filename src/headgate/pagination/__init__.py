# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""Pagination: the strategies a paginated endpoint is followed with, and the lazy result of following it.

``client.paginate(url, pager=Cursor(reader(Page), ...))`` returns a :class:`~headgate.Paginated`, iterated with
``async for``. The strategies (:class:`Cursor`, :class:`Offset`, :class:`PageNumber`, :class:`LinkHeader`,
:class:`NextUrl`, and :class:`BodyCursor` and :class:`BodyOffset` for a walk of POSTs) implement :class:`Pager`, which a
custom scheme implements too, and those that can carry a body :class:`WritingPager`, whose ``write`` encodes it.
"""

from headgate._core import rehome as _rehome
from headgate.pagination._pager import (
    BodyCursor,
    BodyOffset,
    Cursor,
    LinkHeader,
    NextUrl,
    Offset,
    PageNumber,
    Pager,
    PageRequest,
    WritingPager,
)

__all__ = [
    "BodyCursor",
    "BodyOffset",
    "Cursor",
    "LinkHeader",
    "NextUrl",
    "Offset",
    "PageNumber",
    "PageRequest",
    "Pager",
    "WritingPager",
]

_rehome(__name__, globals())
