# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""Origins (RFC 6454 / RFC 9110 section 4.3.1): the scheme, host and effective port a URL addresses.

``httpx2.Origin`` does the normalization (lower-case scheme and host, the default port filled in, IPv6 in canonical
form); these helpers name the operations every layer that keys or compares by origin needs.
"""

from __future__ import annotations

from typing import Final

import httpx2

__all__ = ["origin_of", "origin_text", "same_origin"]

#: The port each scheme implies, left out of :func:`origin_text`.
_DEFAULT_PORTS: Final = {"http": 80, "https": 443, "ws": 80, "wss": 443}


def origin_of(url: httpx2.URL | str) -> httpx2.Origin:
    """Return *url*'s origin.

    :raises ValueError: for a relative URL, which has none.
    """
    return httpx2.Origin(url)


def origin_text(origin: httpx2.Origin) -> str:
    """Return *origin* as ``scheme://host[:port]``, leaving out the scheme's default port and bracketing IPv6."""
    host = f"[{origin.host}]" if ":" in origin.host else origin.host
    port = "" if origin.port in {None, _DEFAULT_PORTS.get(origin.scheme)} else f":{origin.port}"
    return f"{origin.scheme}://{host}{port}"


def same_origin(a: httpx2.URL | str, b: httpx2.URL | str) -> bool:
    """Return whether two absolute URLs address the same origin."""
    return origin_of(a) == origin_of(b)
