# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""``cache_of`` and ``budget_for``: what a client was built with, read back without its private attributes."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from headgate import DEFAULT_TIMEOUTS, HttpCache, Rate, RateBudget, RateRule, Timeouts, TransportPolicy
from headgate.testing import budget_for, cache_of
from tests.support.clients import StubClient

if TYPE_CHECKING:
    from hishel import AsyncSqliteStorage

_BASE = "https://api.example/v1/"
_CANDLES = RateRule("candles", Rate.per_second(1))


def test_the_cache_a_client_was_built_with_is_read_back(storage: AsyncSqliteStorage) -> None:
    """Return the client's ``HttpCache``, whose rules say what it stores; ``None`` without one."""
    cache = HttpCache(storage=storage, ttl=60.0, paths=["series"])
    client = StubClient(base_url=_BASE, policy=TransportPolicy(cache=cache))

    assert cache_of(client) is cache
    assert cache_of(client.with_options(timeout=Timeouts(read=5.0))) is cache
    assert cache.caches_request("GET", "/v1/series/GDP")
    assert not cache.caches_request("GET", "/v1/other")
    assert cache_of(StubClient(base_url=_BASE)) is None


def test_the_budget_a_request_spends_from_is_read_back() -> None:
    """Return the default budget without a URL, and the budget a rule names for a URL its path matches."""
    shared = RateBudget(Rate.per_second(2))
    client = StubClient(base_url=_BASE, rate=shared, policy=TransportPolicy(rate_limits=(_CANDLES,)))

    assert budget_for(client) is shared
    assert budget_for(client, "products/BTC-USD") is shared
    candles = budget_for(client, "products/BTC-USD/candles")
    assert candles is not shared
    assert candles.rate == _CANDLES.rate
    assert budget_for(client, "https://api.example/v1/products/BTC-USD/candles") is candles


def test_a_rate_becomes_the_clients_own_budget() -> None:
    """Build a client's own budget from a bare ``Rate``, as its rate layer spends it."""
    rate = Rate.per_minute(6)
    assert budget_for(StubClient(base_url=_BASE, rate=rate)).rate == rate


def test_a_client_whose_init_never_ran_is_refused() -> None:
    """Name the missing ``super().__init__()`` rather than fail on a missing attribute."""

    class _Unbuilt(StubClient):
        def __init__(self) -> None:
            """Build nothing."""

    with pytest.raises(TypeError, match=r"^_Unbuilt was never built: its __init__ must call super\(\).__init__\(\)$"):
        cache_of(_Unbuilt())


def test_the_default_timeouts_set_every_phase() -> None:
    """Resolve every phase a ``Timeouts`` leaves unset, as a client does."""
    resolved = Timeouts(read=60.0).over(DEFAULT_TIMEOUTS)
    assert (resolved.connect, resolved.read, resolved.write, resolved.pool) == (10.0, 60.0, 10.0, 15.0)
