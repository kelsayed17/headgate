# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""Read a client's breakers from a test, through :func:`headgate.testing.breakers_of` and the health snapshot."""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx2

from headgate.health import breaker_snapshots
from headgate.testing import breakers_of

if TYPE_CHECKING:
    from headgate import Client
    from headgate.breaker import BreakerRegistry
    from headgate.health import BreakerSnapshot


def failures(client: Client, host: str) -> int:
    """Return the consecutive failures *client*'s breakers for *host* hold, on any scheme or port."""
    return sum(snapshot.failures for origin, snapshot in _states(client).items() if httpx2.URL(origin).host == host)


def has_breaker(client: Client, host: str) -> bool:
    """Return whether *client*'s registry holds a breaker for *host*, on any scheme or port."""
    return any(httpx2.URL(origin).host == host for origin in _states(client))


def _states(client: Client) -> dict[str, BreakerSnapshot]:
    """Return a snapshot per origin of *client*'s breakers, none for a client without a breaker."""
    registry = breakers_of(client)
    return {} if registry is None else breaker_snapshots(registry)


def registry_of(client: Client) -> BreakerRegistry:
    """Return the registry *client*'s breakers live in, which a client with a breaker always has."""
    registry = breakers_of(client)
    assert registry is not None, "the client has no breaker"
    return registry
