# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""Response helpers for tests.

``response`` builds a complete ``httpx2.Response`` to hand a reader directly. ``RESPONSE`` is a reader that hands a
test the whole response. It is test-only by design: the library has no reader that returns the raw response, so no
call can receive an undeclared status unread.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx2

if TYPE_CHECKING:
    from headgate._responses import Reader


def response(content: bytes = b"", *, status: int = 200, headers: dict[str, str] | None = None) -> httpx2.Response:
    """Return a complete ``httpx2.Response`` to hand a reader under test."""
    return httpx2.Response(status, content=content, headers=headers)


def _whole(resp: httpx2.Response, /) -> httpx2.Response:
    """Return *resp* itself."""
    return resp


RESPONSE: Reader[httpx2.Response] = _whole
