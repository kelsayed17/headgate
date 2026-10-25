# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""The deadline: one bound on a whole logical call, from the policy, a route, or the call itself.

A network that never answers stands in for every way a call can run long (a hung read, retries, limiter waits): the
deadline cancels whatever is in flight and the call raises ``ClientTimeoutError``. The cancellation is the caller's
budget running out, not the host failing, so the breaker records nothing.
"""

from __future__ import annotations

import asyncio
from typing import override

import httpx2
import pytest

from headgate import ClientTimeoutError, HttpOptions, Rate, Route, TransportPolicy, nothing
from headgate.retry import NO_RETRY
from tests.support.breakers import failures
from tests.support.clients import StubClient

_HOST = "slow.example.test"
_SHORT = 0.05
#: How long the slow network takes to answer: longer than the short deadline, short enough not to slow the suite.
_SLOW = 0.15


class _Network(httpx2.AsyncBaseTransport):
    """A network that answers 200 after *delay* seconds of real time."""

    def __init__(self, delay: float) -> None:
        """Answer after *delay*."""
        self._delay = delay

    @override
    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        await asyncio.sleep(self._delay)
        return httpx2.Response(200)


def _client(policy: TransportPolicy, delay: float = 3600.0) -> StubClient:
    """Build a client over a network that answers after *delay* seconds."""
    return StubClient(
        base_url=f"https://{_HOST}",
        rate=Rate.per_second(1000),
        policy=policy,
        http=HttpOptions(transport=_Network(delay)),
    )


async def test_the_policy_deadline_bounds_a_call_that_hangs() -> None:
    """Raise ``ClientTimeoutError`` naming the request once the policy's deadline passes, and charge no host."""
    client = _client(TransportPolicy(deadline=_SHORT, retry=NO_RETRY))

    with pytest.raises(ClientTimeoutError, match="deadline exceeded") as raised:
        await client.get("series", expect=nothing)

    error = raised.value
    assert (error.method, error.url, error.attempts) == ("GET", "series", 1)
    assert error.retryable is False
    assert failures(_HOST) == 0, "the caller's budget ran out; the host said nothing"


async def test_a_route_deadline_applies_to_its_paths() -> None:
    """Bound a matching path by its route's deadline, though the policy allows longer."""
    client = _client(TransportPolicy(routes=(Route("/reports", deadline=_SHORT),)))

    with pytest.raises(ClientTimeoutError, match="deadline exceeded"):
        await client.get("reports/daily", expect=nothing)


async def test_a_calls_own_deadline_wins() -> None:
    """Bound a call by its own ``deadline=``, over its route's and the policy's."""
    client = _client(TransportPolicy(routes=(Route("/reports", deadline=60.0),)))

    with pytest.raises(ClientTimeoutError, match="deadline exceeded"):
        await client.get("reports/daily", expect=nothing, deadline=_SHORT)


async def test_deadline_none_lifts_the_policy_deadline_for_one_call() -> None:
    """Let a call with ``deadline=None`` run past the policy's deadline."""
    client = _client(TransportPolicy(deadline=_SHORT), delay=_SLOW)

    await client.get("series", expect=nothing, deadline=None)

    with pytest.raises(ClientTimeoutError):
        await client.get("series", expect=nothing)


@pytest.mark.parametrize("deadline", [0.0, -1.0, float("nan")])
async def test_a_calls_deadline_must_be_positive(deadline: float) -> None:
    """Refuse a call's deadline that is not positive, before anything is sent."""
    client = _client(TransportPolicy())
    with pytest.raises(ValueError, match="deadline"):
        await client.get("series", expect=nothing, deadline=deadline)
