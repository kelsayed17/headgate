# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""Read a client's breakers from a test, through :func:`headgate.testing.breakers_of`."""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx2

from headgate.testing import breakers_of

if TYPE_CHECKING:
    from headgate import AsyncBaseClient
    from headgate.breaker import BreakerRegistry


def failures(client: AsyncBaseClient, host: str) -> int:
    """Return the consecutive failures *client*'s breakers for *host* hold, on any scheme or port."""
    registry = breakers_of(client)
    return sum(breaker.failure_count for breaker in registry or () if httpx2.URL(breaker.name).host == host)


def has_breaker(client: AsyncBaseClient, host: str) -> bool:
    """Return whether *client*'s registry holds a breaker for *host*, on any scheme or port."""
    return any(httpx2.URL(breaker.name).host == host for breaker in breakers_of(client) or ())


def registry_of(client: AsyncBaseClient) -> BreakerRegistry:
    """Return the registry *client*'s breakers live in, which a client with a breaker always has."""
    registry = breakers_of(client)
    assert registry is not None, "the client has no breaker"
    return registry
