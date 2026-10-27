# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""``with_options``: a view of a client with other call defaults, sharing its pool, stack and breakers."""

from __future__ import annotations

import asyncio
import math

import httpx2
import pytest

from headgate import (
    BreakerPolicy,
    HttpOptions,
    RetryPolicy,
    Route,
    Timeouts,
    TransportPolicy,
    as_bytes,
    nothing,
)
from headgate.errors import CircuitOpenError, ClientTimeoutError, UpstreamStatusError
from headgate.policy import policy_disabled
from headgate.retry import NO_RETRY
from headgate.testing import FakeTransport, isolated_breakers
from tests.observe._support import BASE, RATE, client
from tests.support.clients import StubClient

#: Long enough that a short deadline expires first, short enough to keep the suite fast when one does not.
_SLOW_SEND = 0.05
_SHORT_DEADLINE = 0.01


def _sent_timeout(request: httpx2.Request) -> dict[str, float]:
    """Return the per-phase timeouts *request* was sent with."""
    timeout = request.extensions["timeout"]
    assert isinstance(timeout, dict)
    return {str(phase): float(seconds) for phase, seconds in timeout.items()}


async def test_view_headers_go_under_the_calls_and_over_the_clients() -> None:
    """Send the client's headers, the view's over them, and the call's over both."""
    fake = FakeTransport().reply("GET", "/v1/x")
    root = client(fake, http=HttpOptions(headers={"X-Client": "c", "X-Shared": "client"}))
    view = root.with_options(headers={"X-View": "v", "X-Shared": "view", "X-Call": "view"})
    await view.get("x", expect=nothing, headers={"x-call": "call"})

    headers = fake.calls[0].headers
    assert (headers["x-client"], headers["x-view"], headers["x-shared"], headers["x-call"]) == (
        "c",
        "v",
        "view",
        "call",
    )


async def test_a_view_of_a_view_layers_its_headers() -> None:
    """Merge a further view's headers over the outer view's, case-insensitively."""
    fake = FakeTransport().reply("GET", "/v1/x")
    view = client(fake).with_options(headers={"X-A": "1", "X-B": "1"}).with_options(headers={"x-b": "2"})
    await view.get("x", expect=nothing)
    assert (fake.calls[0].headers["x-a"], fake.calls[0].headers.get_list("x-b")) == ("1", ["2"])


async def test_the_client_itself_is_unchanged() -> None:
    """Leave the client's own calls without the view's headers."""
    fake = FakeTransport().reply("GET", "/v1/x")
    root = client(fake)
    root.with_options(headers={"X-View": "v"}, retry=False, deadline=None, timeout=Timeouts(read=1.0))
    await root.get("x", expect=nothing)
    assert "x-view" not in fake.calls[0].headers


async def test_a_view_replaces_only_the_phases_it_sets() -> None:
    """Keep the client's other timeouts and replace the one the view sets."""
    fake = FakeTransport().reply("GET", "/v1/x")
    root = client(fake, http=HttpOptions(timeout=Timeouts(connect=1.0, read=2.0, write=3.0, pool=4.0)))
    await root.with_options(timeout=Timeouts(read=9.0)).get("x", expect=nothing)
    assert _sent_timeout(fake.calls[0]) == {"connect": 1.0, "read": 9.0, "write": 3.0, "pool": 4.0}


async def test_each_layer_replaces_the_phases_it_sets_and_a_call_wins() -> None:
    """Lay the view's phases over the client's and the defaults; a call's own phases then win over both."""
    fake = FakeTransport().reply("GET", "/v1/x")
    view = client(fake, http=HttpOptions(timeout=Timeouts(read=5.0))).with_options(
        timeout=Timeouts(connect=1.0, read=2.0)
    )
    await view.get("x", expect=nothing)
    await view.get("x", expect=nothing, timeout=Timeouts(read=7.0))
    first, second = (_sent_timeout(request) for request in fake.calls)
    assert first == {"connect": 1.0, "read": 2.0, "write": 10.0, "pool": 15.0}
    assert second == {**first, "read": 7.0}


async def test_phases_no_layer_sets_keep_the_defaults() -> None:
    """Send the default timeout for every phase that neither the client nor the view sets."""
    fake = FakeTransport().reply("GET", "/v1/x")
    root = client(fake, http=HttpOptions(timeout=Timeouts(read=5.0)))
    await root.with_options(timeout=Timeouts(read=6.0)).get("x", expect=nothing)
    assert _sent_timeout(fake.calls[0]) == {"connect": 10.0, "read": 6.0, "write": 10.0, "pool": 15.0}


async def _slow(request: httpx2.Request) -> httpx2.Response:
    """Answer after :data:`_SLOW_SEND` seconds."""
    await asyncio.sleep(_SLOW_SEND)
    return httpx2.Response(200, request=request)


async def test_a_view_deadline_bounds_its_calls() -> None:
    """Raise ``ClientTimeoutError`` once the view's deadline passes."""
    fake = FakeTransport().reply("GET", "/v1/x", _slow)
    with pytest.raises(ClientTimeoutError):
        await client(fake).with_options(deadline=_SHORT_DEADLINE).get("x", expect=nothing)


async def test_a_view_deadline_of_none_lifts_the_policy_and_route_deadlines() -> None:
    """Run a call past the policy's and the route's deadlines when the view has none."""
    fake = FakeTransport().reply("GET", "/v1/x", _slow)
    policy = TransportPolicy(deadline=_SHORT_DEADLINE, routes=(Route("/x", deadline=_SHORT_DEADLINE),))
    await client(fake, policy=policy).with_options(deadline=None).get("x", expect=nothing)


async def test_a_calls_deadline_wins_over_the_views() -> None:
    """Apply the call's ``deadline=`` over the view's."""
    fake = FakeTransport().reply("GET", "/v1/x", _slow)
    view = client(fake).with_options(deadline=None)
    with pytest.raises(ClientTimeoutError):
        await view.get("x", expect=nothing, deadline=_SHORT_DEADLINE)


@pytest.mark.parametrize("deadline", [0.0, -1.0, math.nan, math.inf])
def test_a_bad_deadline_is_refused(deadline: float) -> None:
    """Refuse a deadline that is not positive and finite."""
    with pytest.raises(ValueError, match="deadline must be > 0"):
        client(FakeTransport()).with_options(deadline=deadline)


def test_a_bad_timeout_is_refused() -> None:
    """Refuse a timeout that is not positive."""
    with pytest.raises(ValueError, match="read must be > 0"):
        client(FakeTransport()).with_options(timeout=Timeouts(read=0.0))


async def test_retry_false_sends_once() -> None:
    """Send a view's calls once when it says ``retry=False``."""
    fake = FakeTransport().reply("GET", "/v1/x", 503, 200)
    with pytest.raises(UpstreamStatusError):
        await client(fake).with_options(retry=False).get("x", expect=nothing)
    assert len(fake.calls) == 1


async def test_a_view_policy_wins_over_the_routes_and_loses_to_the_calls() -> None:
    """Retry under the view's policy despite a no-retry route, and not at all when the call says so."""
    fake = FakeTransport().reply("GET", "/v1/x", 503, 200)
    policy = TransportPolicy(routes=(Route("/x", retry=NO_RETRY),))
    view = client(fake, policy=policy).with_options(retry=RetryPolicy(attempts=2))
    await view.get("x", expect=nothing)
    assert len(fake.calls) == len(("503", "200"))

    fake.reply("GET", "/v1/x", 503, 200)
    with pytest.raises(UpstreamStatusError):
        await view.get("x", expect=nothing, retry=False)


async def test_retry_true_restores_the_clients_policy() -> None:
    """Undo an outer view's ``retry=False`` with ``retry=True``."""
    fake = FakeTransport().reply("GET", "/v1/x", 503, 200)
    await client(fake).with_options(retry=False).with_options(retry=True).get("x", expect=nothing)
    assert len(fake.calls) == len(("503", "200"))


async def test_a_view_cannot_retry_a_client_built_with_its_policy_off() -> None:
    """Keep sending once on a client built under ``policy_disabled``, whatever the view asks."""
    fake = FakeTransport().reply("GET", "/v1/x", 503, 200)
    with policy_disabled():
        root = client(fake)
    with pytest.raises(UpstreamStatusError):
        await root.with_options(retry=RetryPolicy(attempts=3)).get("x", expect=nothing)


async def test_a_view_shares_the_breakers() -> None:
    """Refuse a view's call once the client's own calls opened the host's breaker."""
    fake = FakeTransport().reply("GET", "/v1/x", 503)
    policy = TransportPolicy(retry=NO_RETRY, breaker=BreakerPolicy(failure_threshold=1))
    with isolated_breakers():
        root = client(fake, policy=policy)
    with pytest.raises(UpstreamStatusError):
        await root.get("x", expect=nothing)
    with pytest.raises(CircuitOpenError):
        await root.with_options(headers={"X-View": "v"}).get("x", expect=nothing)


async def test_closing_a_view_closes_nothing_and_the_client_closes_the_pool() -> None:
    """Leave the pool open when a view is closed or its block exits; close it with the client."""
    fake = FakeTransport().reply("GET", "/v1/x", httpx2.Response(200, content=b"ok"))
    root = client(fake)
    async with root.with_options(retry=False) as view:
        assert await view.get("x", expect=as_bytes) == b"ok"
    await view.aclose()
    assert not fake.closed
    assert await root.get("x", expect=as_bytes) == b"ok"

    await root.aclose()
    assert fake.closed


class _Counting(StubClient):
    """A client with state of its own, which views share."""

    def __init__(self, transport: httpx2.AsyncBaseTransport) -> None:
        """Build over *transport* with an empty log."""
        super().__init__(base_url=BASE, rate=RATE, http=HttpOptions(transport=transport))
        self.log: list[str] = []

    async def fetch(self) -> None:
        """Make one call, logging it."""
        self.log.append("fetch")
        await self.get("x", expect=nothing)


async def test_a_view_is_the_same_subclass_sharing_its_state() -> None:
    """Return the subclass, its methods using the view's defaults and its attributes shared."""
    fake = FakeTransport().reply("GET", "/v1/x")
    root = _Counting(fake)
    view = root.with_options(headers={"X-View": "v"})
    await view.fetch()

    assert isinstance(view, _Counting)
    assert root.log == ["fetch"]
    assert fake.calls[0].headers["x-view"] == "v"
