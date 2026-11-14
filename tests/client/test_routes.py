# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""``Route``, the one per-path rule: a call's first matching route is its route, whole, at every layer.

A route sets a call's retry policy, deadline, rate budget and caching. What it leaves out comes from the policy and the
client's budget, and its ``None`` is off. The call's context carries what the route set to every layer below, so a
redirect's later hops keep the call's route.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final, cast

import httpx2
import pytest

from headgate import (
    HttpCache,
    HttpOptions,
    Rate,
    RateBudget,
    RetryPolicy,
    Route,
    TransportPolicy,
    UpstreamStatusError,
    as_bytes,
    nothing,
)
from headgate._core import CallContext
from headgate.errors import RateLimitError
from headgate.ratelimit._budgets import CALL_BUDGET
from headgate.testing import budget_for
from tests.cache._helpers import every_get_rules, stub_client
from tests.support.clients import StubClient
from tests.support.wire import Script

if TYPE_CHECKING:
    from hishel import AsyncSqliteStorage

_BASE: Final = "https://api.example.test"
_RATE: Final = Rate.per_second(1000)
_THREE: Final = RetryPolicy(attempts=3)


def _client(
    script: Script, *routes: Route, retry: RetryPolicy | None = _THREE, deadline: float | None = 60.0
) -> StubClient:
    """Return a client over *script* with *routes*, a policy *retry* and *deadline*, and no breaker."""
    return StubClient(
        base_url=_BASE,
        rate=_RATE,
        policy=TransportPolicy(retry=retry, deadline=deadline, breaker=None, routes=routes),
        http=HttpOptions(transport=script.transport()),
    )


def _budget_of(request: httpx2.Request) -> RateBudget | None:
    """Return the budget *request*'s call spends from, as its context names it."""
    context = CallContext.find(request)
    return None if context is None else context.get(CALL_BUDGET)


# One route, whole, at every layer.


async def test_a_route_sets_its_paths_budget_and_leaves_their_retries_to_the_policy() -> None:
    """Spend a matching path's sends from its route's budget, retried under the policy's three attempts."""
    script = Script(503, 503, 200)
    client = _client(script, Route("/exports", rate=Rate.per_second(1)))

    await client.get("exports/a", expect=nothing)

    exports = budget_for(client, "exports/a")
    assert exports is not budget_for(client)
    assert [_budget_of(request) for request in script.sent] == [exports] * len(("503", "503", "200"))


async def test_the_first_matching_route_is_the_call_s_route_whole() -> None:
    """Take a path whole from the first route it matches: sent once, from the client's budget, not the later route's."""
    script = Script(503, 200)
    client = _client(script, Route("/exports/big", retry=None), Route("/exports", rate=Rate.per_second(1)))

    with pytest.raises(UpstreamStatusError):
        await client.get("exports/big/file", expect=nothing)

    assert [_budget_of(request) for request in script.sent] == [budget_for(client)]


async def test_a_redirects_later_hops_keep_the_call_s_route() -> None:
    """Spend a redirect's second hop, to a path no route names, from the budget of the call's route."""
    moved = httpx2.Response(302, headers={"Location": f"{_BASE}/other"})
    script = Script(moved, 200)
    client = _client(script, Route("/exports", rate=Rate.per_second(1)))

    await client.get("exports/a", expect=nothing)

    exports = budget_for(client, "exports/a")
    assert [request.url.path for request in script.sent] == ["/exports/a", "/other"]
    assert [_budget_of(request) for request in script.sent] == [exports, exports]
    assert budget_for(client, "other") is budget_for(client)


async def test_a_hop_to_another_origin_keeps_the_route_but_not_its_pause() -> None:
    """Spend a cross-origin hop's turn from the call's route budget, yet let only the client's own origin pause it.

    The redirect target's 429 ``Retry-After`` answers this one call; the budget, which every call on the route shares,
    stays unpaused (review finding M11).
    """
    moved = httpx2.Response(302, headers={"Location": "https://cdn.example/exports/a"})
    refused = httpx2.Response(429, headers={"Retry-After": "30"})
    script = Script(moved, refused)
    client = _client(script, Route("/exports", rate=Rate.per_second(1000)), retry=None)

    with pytest.raises(RateLimitError):
        await client.get("exports/a", expect=nothing)

    exports = budget_for(client, "exports/a")
    assert [request.url.host for request in script.sent] == ["api.example.test", "cdn.example"]
    assert [_budget_of(request) for request in script.sent] == [exports, exports]
    assert exports.pause_remaining == 0


# None is off; leaving an option out inherits.


async def test_a_routes_retry_none_sends_once_and_absence_inherits() -> None:
    """Send once on a route that says ``retry=None``, and three times on one that leaves ``retry`` out."""
    once, inherited = Script(503), Script(503)

    with pytest.raises(UpstreamStatusError):
        await _client(once, Route("/x", retry=None)).get("x", expect=nothing)
    with pytest.raises(UpstreamStatusError):
        await _client(inherited, Route("/x")).get("x", expect=nothing)

    assert (len(once.sent), len(inherited.sent)) == (1, _THREE.attempts)


async def test_a_routes_deadline_none_has_none_and_absence_inherits() -> None:
    """Run a call with no deadline on a route that says ``deadline=None``, and under the policy's one otherwise."""
    script = Script()
    client = _client(script, Route("/none", deadline=None), Route("/kept"), deadline=1.0)

    await client.get("none", expect=nothing)
    await client.get("kept", expect=nothing)

    contexts = [CallContext.find(request) for request in script.sent]
    assert [context is not None and context.deadline is None for context in contexts] == [True, False]


async def test_a_routes_rate_none_paces_nothing_yet_still_takes_the_servers_pause() -> None:
    """Give a ``rate=None`` route an unpaced budget of its own, which a 429's ``Retry-After`` still pauses."""
    script = Script(httpx2.Response(429, headers={"Retry-After": "30"}))
    client = _client(script, Route("/free", rate=None), retry=None)

    with pytest.raises(RateLimitError):
        await client.get("free", expect=nothing)

    free = budget_for(client, "free")
    assert free is not budget_for(client)
    assert free.rate == Rate.per_second(1_000_000)
    assert free.pause_remaining > 0
    assert budget_for(client).pause_remaining == 0


async def test_a_routes_cache_none_keeps_its_paths_out_of_the_cache(storage: AsyncSqliteStorage) -> None:
    """Send every call on a ``cache=None`` route to the network, and serve a route that leaves ``cache`` out from it."""
    script = Script(httpx2.Response(200, content=b"body"))
    policy = TransportPolicy(routes=(Route("/live", cache=None), Route("/stored")))
    client = stub_client(
        transport=script.transport(), cache=HttpCache(**every_get_rules(), storage=storage), policy=policy
    )

    for path in ("live", "live", "stored", "stored"):
        assert await client.get(path, expect=as_bytes) == b"body"

    assert [request.url.path for request in script.sent] == ["/live", "/live", "/stored"]


# A route is a value.


def test_routes_compare_and_hash_by_fragment_and_options() -> None:
    """Treat two routes with one fragment and the same options as equal, a shared budget compared by identity."""
    shared = RateBudget(Rate.per_second(1))

    assert Route("/a", rate=shared, deadline=5.0) == Route("/a", deadline=5.0, rate=shared)
    assert hash(Route("/a", rate=shared)) == hash(Route("/a", rate=shared))
    assert Route("/a", rate=shared) != Route("/a", rate=RateBudget(Rate.per_second(1)))
    assert Route("/a") != Route("/b")
    assert Route("/a") != "/a"
    assert Route("/a", retry=None).options == {"retry": None}


def test_a_route_reads_as_it_was_written() -> None:
    """Show the fragment and only the options the route set."""
    assert repr(Route("/exports", deadline=300.0)) == "Route('/exports', deadline=300.0)"
    assert repr(Route("/a")) == "Route('/a')"


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"rates": 1}, r"^Route\(\) got an unexpected keyword argument 'rates'$"),
        ({"retry": False}, r"^retry= takes a RetryPolicy, or None to send once \(got bool\): write retry=None"),
        ({"retry": 3}, r"^retry= takes a RetryPolicy, or None to send once \(got int\)$"),
        ({"rate": 10}, r"^rate= takes a Rate, a RateBudget or None \(got int\)$"),
        ({"cache": False}, r"^cache= takes None, to keep these paths out of the cache \(got bool\)$"),
    ],
    ids=["unknown", "retry-false", "retry-int", "rate-int", "cache-false"],
)
def test_a_route_refuses_an_option_it_does_not_take(options: dict[str, Any], message: str) -> None:
    """Refuse an unknown option, and a value of the wrong kind, where the route is written."""
    with pytest.raises(TypeError, match=message):
        Route("/a", **options)


def test_a_policy_takes_only_routes() -> None:
    """Refuse a bare fragment, or a fragment string, where a route goes."""
    message = r"^routes= takes a tuple of Route, such as \(Route\('/exports', deadline=300\.0\),\) \(got str\)$"
    with pytest.raises(TypeError, match=message):
        TransportPolicy(routes=cast("Any", ("/x",)))
    with pytest.raises(TypeError, match=message):
        TransportPolicy(routes=cast("Any", "/x"))
