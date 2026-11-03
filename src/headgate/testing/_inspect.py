# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""What a client was built with, read back: its cache, and the rate budget a request spends from.

A client fixes its options in ``super().__init__()``, so its tests check what it configured by reading it back: the
cache it keeps (and, through :meth:`~headgate.HttpCache.filter_policy`, what that stores) and the rate budget a path
spends from. Neither sends anything.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from headgate._client import built_with

if TYPE_CHECKING:
    from headgate import AsyncBaseClient, HttpCache
    from headgate.ratelimit import RateBudget

__all__ = ["budget_for", "cache_of"]


def cache_of(client: AsyncBaseClient) -> HttpCache | None:
    """Return the :class:`~headgate.HttpCache` *client* was built with (``TransportPolicy(cache=...)``), or ``None``."""
    return built_with(client).cache


def budget_for(client: AsyncBaseClient, url: str | None = None) -> RateBudget:
    """Return the :class:`~headgate.RateBudget` a request to *url* spends from, as the client's rate layer chooses it.

    *url* is joined to the client's ``base_url`` as a verb's is, and the first :class:`~headgate.RateRule` whose
    fragment its path holds names the budget. Without a *url*, it is the client's default budget: the one every request
    that no rule names spends from, built from ``rate=``. A :class:`~headgate.RateBudget` passed as ``rate=`` is
    returned as it is, so two clients sharing it return the same object.
    """
    built = built_with(client)
    if url is None:
        return built.budgets.default
    return built.budgets.for_request(built.http.build_request("GET", url))
