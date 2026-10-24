"""Test-only response helpers for the clients member — never shipped.

``response`` builds a complete ``httpx2.Response`` to hand a reader directly. ``RESPONSE`` is a reader that hands a
test the whole response, for the tests that used to read one off raw mode. It is test-only by design: a client method
returning an ``httpx2.Response`` is what raw mode was. It must not appear in ``src/``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx2

if TYPE_CHECKING:
    from headgate._responses import Reader


def response(content: bytes = b"", *, status: int = 200, headers: dict[str, str] | None = None) -> httpx2.Response:
    """A complete ``httpx2.Response`` for a reader under test."""
    return httpx2.Response(status, content=content, headers=headers)


def _whole(resp: httpx2.Response, /) -> httpx2.Response:
    return resp


RESPONSE: Reader[httpx2.Response] = _whole
