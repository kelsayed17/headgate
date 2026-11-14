# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""The deadline: one bound on a whole logical call, from the policy, a route, or the call itself.

A network that never answers stands in for every way a call can run long (a hung read, retries, rate-budget waits): the
deadline cancels whatever is in flight and the call raises ``ClientTimeoutError``. An attempt it cuts off on the
network is a host that did not answer in time, so the breaker counts it as a timeout.
"""

from __future__ import annotations

import asyncio
from typing import override

import httpx2
import pytest

from headgate import Client, ClientTimeoutError, HttpOptions, Rate, Route, TransportPolicy, nothing
from headgate.testing import TestClock
from tests.support.breakers import failures

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


def _client(policy: TransportPolicy, delay: float = 3600.0) -> Client:
    """Build a client over a network that answers after *delay* seconds."""
    return Client(
        base_url=f"https://{_HOST}",
        rate=Rate.per_second(1000),
        policy=policy,
        http=HttpOptions(transport=_Network(delay)),
    )


async def test_the_policy_deadline_bounds_a_call_that_hangs() -> None:
    """Raise ``ClientTimeoutError`` naming the request once the policy's deadline passes, and charge the host."""
    client = _client(TransportPolicy(deadline=_SHORT, retry=None))

    with pytest.raises(ClientTimeoutError, match="deadline exceeded") as raised:
        await client.get("series", expect=nothing)

    error = raised.value
    assert (error.method, error.url, error.attempts) == ("GET", "series", 1)
    assert error.retryable is False
    assert failures(client, _HOST) == 1, "the host never answered the attempt the deadline cut off"


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


class _HourLongNetwork(httpx2.AsyncBaseTransport):
    """A network that lets *spent* seconds pass on *clock*, then never answers."""

    def __init__(self, clock: TestClock, spent: float) -> None:
        """Advance *clock* by *spent* on each send."""
        self._clock = clock
        self._spent = spent

    @override
    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        self._clock.advance(self._spent)
        await asyncio.sleep(3600)
        raise AssertionError  # pragma: no cover - the deadline cancels the sleep


@pytest.mark.parametrize(
    ("spent", "written"),
    [
        pytest.param(1234.0, "1234s", id="long"),
        pytest.param(2.5, "2.5s", id="fraction"),
        pytest.param(0.0, "0s", id="none"),
        pytest.param(1.004, "1s", id="rounded"),
    ],
)
async def test_the_deadline_message_writes_the_elapsed_time_plainly(spent: float, written: str) -> None:
    """Write the elapsed time to the hundredth, never in scientific notation, without trailing zeros."""
    with TestClock().install() as clock:
        client = Client(
            base_url=f"https://{_HOST}",
            rate=Rate.per_second(1000),
            policy=TransportPolicy(deadline=_SHORT, retry=None),
            http=HttpOptions(transport=_HourLongNetwork(clock, spent)),
        )
        with pytest.raises(ClientTimeoutError) as raised:
            await client.get("series", expect=nothing)

    assert str(raised.value).endswith(f"deadline exceeded after {written}")
