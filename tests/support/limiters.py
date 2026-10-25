# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""Find a client's rate limiters in its transport stack.

The limiters live in the stack's :class:`~headgate._rate_limit.RateLimitTransport`, under retry and, when the client
caches, under the cache transport too. Neither ``AsyncBaseClient`` nor ``httpx2.AsyncClient`` exposes its transport,
and each layer names the one below it differently, so the stack is searched through the object graph
(:func:`gc.get_referents`) rather than by private attribute names. The search follows only instance dictionaries,
transports and httpx2 clients, so it cannot wander into module globals and find another client's limiter.
"""

from __future__ import annotations

import gc
from typing import TYPE_CHECKING

import httpx2

from headgate._rate_limit import RateLimitTransport

if TYPE_CHECKING:
    from headgate._client import AsyncBaseClient
    from headgate._rate_limit import AsyncRateLimiter


def _is_stack_node(obj: object) -> bool:
    """Whether the search may pass through *obj*: a transport, an httpx2 client, or an instance's attributes."""
    return isinstance(obj, (dict, httpx2.AsyncBaseTransport, httpx2.AsyncClient))


def rate_limit_transport(client: AsyncBaseClient) -> RateLimitTransport:
    """Return the one :class:`RateLimitTransport` in *client*'s transport stack.

    Raises :class:`AssertionError` when the stack holds none, or more than one.
    """
    seen: set[int] = {id(client)}
    frontier: list[object] = [client]
    found: list[RateLimitTransport] = []
    while frontier:
        referents = [obj for obj in gc.get_referents(*frontier) if id(obj) not in seen]
        seen.update(id(obj) for obj in referents)
        found.extend(obj for obj in referents if isinstance(obj, RateLimitTransport))
        frontier = [obj for obj in referents if _is_stack_node(obj) and not isinstance(obj, RateLimitTransport)]
    if len(found) != 1:
        msg = f"expected one RateLimitTransport in {type(client).__name__}'s stack, found {len(found)}"
        raise AssertionError(msg)
    return found[0]


def default_limiter(client: AsyncBaseClient) -> AsyncRateLimiter:
    """Return the limiter *client* paces a request on when no ``rate_limits`` rule matches it."""
    return rate_limit_transport(client).limiters.default
