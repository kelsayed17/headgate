"""Find a client's rate limiters in its transport stack (the seam that replaced ``client._rate_limiter``).

The limiters live in the stack's :class:`~headgate._transports.RateLimitTransport`, under retry and, when the
client caches, under the cache transport too. Walking the stack keeps tests off a client attribute that production code
never needed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from headgate._rate_limit import RateLimitTransport

if TYPE_CHECKING:
    from headgate._client import AsyncBaseClient
    from headgate._rate_limit import AsyncRateLimiter

#: The attributes one transport layer names the layer below it by: hishel
#: (``next_transport``), the extension restorer (``_inner``) and the rate limiter (``_transport``).
_NEXT = ("next_transport", "_inner", "_transport")


def rate_limit_transport(client: AsyncBaseClient) -> RateLimitTransport:
    layer: object = client._http_client._transport
    for _ in range(8):
        if isinstance(layer, RateLimitTransport):
            return layer
        layer = next((getattr(layer, a) for a in _NEXT if hasattr(layer, a)), None)
    raise AssertionError(f"no RateLimitTransport in {type(client).__name__}'s stack")


def default_limiter(client: AsyncBaseClient) -> AsyncRateLimiter:
    return rate_limit_transport(client)._limiters.default
