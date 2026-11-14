# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""A retry budget is shared only when one ``RetryBudget`` object is passed: a ``RetryShare`` is each client's own.

Every budget here admits one retry over its window (no ratio, a floor of one per window), so the second retry a
budget is asked for shows whether two calls spent one budget or two.
"""

from __future__ import annotations

from typing import Any, Final, LiteralString, cast

import pytest

from headgate import HttpOptions, Rate, RetryPolicy, Route, TransportPolicy, UpstreamStatusError, nothing
from headgate.retry import Backoff, RetryBudget, RetryShare
from tests.support.clients import StubClient
from tests.support.wire import Script

#: One retry per ten-second window.
_ONE_RETRY: Final = RetryShare(ratio=0.0, min_per_second=0.1, window=10.0)


def _client(script: Script, budget: RetryShare | RetryBudget | None, *routes: Route) -> StubClient:
    """Return a client over *script* whose policy retries 503s from *budget*, with *routes*."""
    return StubClient(
        base_url="https://api.example.test",
        rate=Rate.per_second(1000),
        policy=TransportPolicy(retry=RetryPolicy(budget=budget), breaker=None, routes=routes),
        http=HttpOptions(transport=script.transport()),
    )


async def _retried(client: StubClient, path: LiteralString = "x") -> bool:
    """Return whether a call answered 503, then 200, was retried (it succeeds) or refused its retry (it raises)."""
    try:
        await client.get(path, expect=nothing)
    except UpstreamStatusError:
        return False
    return True


@pytest.mark.transport_policy
async def test_two_clients_given_one_retry_budget_spend_one_window() -> None:
    """Refuse the second client's retry once the first spent the budget they were both given."""
    shared = RetryBudget(_ONE_RETRY)
    first, second = _client(Script(503, 200), shared), _client(Script(503, 200), shared)

    assert await _retried(first)
    assert not await _retried(second)


@pytest.mark.transport_policy
@pytest.mark.parametrize("budget", [_ONE_RETRY, None], ids=["share", "none"])
async def test_two_clients_given_a_share_keep_a_budget_each(budget: RetryShare | None) -> None:
    """Let each client retry once from a budget of its own, built from the share each was given (or no cap)."""
    first, second = _client(Script(503, 200), budget), _client(Script(503, 200), budget)

    assert await _retried(first)
    assert await _retried(second)


@pytest.mark.transport_policy
async def test_a_clients_views_and_routes_spend_its_one_budget() -> None:
    """Spend one budget for a client's own calls, its view's and a route's that names an equal share."""
    script = Script(503, 200, 503, 503, 200)
    client = _client(script, _ONE_RETRY, Route("/reports", retry=RetryPolicy(budget=_ONE_RETRY)))

    assert await _retried(client)
    assert not await _retried(client.with_options(headers={"X-View": "v"}))
    assert not await _retried(client, "reports")


def test_a_retry_budget_is_built_from_a_share() -> None:
    """Refuse the old frozen spelling, keywords and all; a value of the wrong kind names ``RetryShare``."""
    with pytest.raises(TypeError):
        RetryBudget(**cast("Any", {"ratio": 0.1}))
    with pytest.raises(TypeError, match=r"^share= takes a RetryShare, such as RetryShare\(ratio=0\.1\) \(got float\)$"):
        RetryBudget(cast("Any", 0.1))
    with pytest.raises(TypeError, match=r"^budget= takes a RetryShare, a shared RetryBudget, or None \(got Backoff\)$"):
        RetryPolicy(budget=cast("Any", Backoff()))


def test_a_retry_budget_shows_its_state() -> None:
    """Name the share and what the window holds now."""
    budget = RetryBudget(_ONE_RETRY)
    budget.record_first()
    assert budget.try_spend()

    assert repr(budget) == f"RetryBudget({_ONE_RETRY!r}, retries=1, firsts=1)"
    assert budget.share is _ONE_RETRY
