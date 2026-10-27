# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""``AsyncBaseClient``'s cache: when it is engaged, hit and miss behavior, TTL threading, and a shared storage.

The cache is opt-in per client (``cache=HttpCache(policy=, storage=, ttl=)``). A client with no ``cache`` never
caches. The ``HttpCache``'s ``ttl`` is stamped on every request as the ``hishel_ttl`` extension by the cache transport
(the ``FilterPolicy``, not the TTL, decides what is stored), and a per-call ``cache_ttl=`` wins over it. Everything
here is observed from the network leg, not the stack's internals.
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime
from typing import TYPE_CHECKING, override

import anysqlite
import httpx2
import pytest
import time_machine

from headgate._client import AsyncBaseClient
from headgate.cache import HttpCache
from headgate.ratelimit import Rate
from tests.cache._helpers import CloseCountingStorage, series_policy, stub_client
from tests.support.responses import RESPONSE

if TYPE_CHECKING:
    from hishel import AsyncSqliteStorage

_CLIENT_TTL = 3600.0
_CALL_TTL = 99.0
_SHORT_TTL = 0.5
_GAP = 2.0
_REPEATS = 3
_SHARING_CLIENTS = 3


class _CountingTransport(httpx2.AsyncBaseTransport):
    """The network leg: counts the requests that reach it and records each one's extensions."""

    def __init__(self) -> None:
        self.count = 0
        self.extensions: list[dict[str, object]] = []

    @override
    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        self.count += 1
        self.extensions.append(dict(request.extensions))
        return httpx2.Response(httpx2.codes.OK, json={"call": self.count})


# When the cache is engaged.


async def test_a_client_without_a_cache_never_caches() -> None:
    """A client built without a ``cache`` sends every repeated GET and POST to the network."""
    base = _CountingTransport()
    client = stub_client(rate=Rate.per_second(2), transport=base)

    for _ in range(_REPEATS):
        await client.get("/series?id=A", expect=RESPONSE)
    assert base.count == _REPEATS
    for _ in range(_REPEATS):
        await client.post("/series", expect=RESPONSE)
    assert base.count == 2 * _REPEATS


# Hit and miss behavior and hishel_ttl threading, over real SQLite storage.


async def test_cache_hit_second_call_no_network(storage: AsyncSqliteStorage) -> None:
    """The second identical GET is served from the cache, without reaching the network, and is marked as a hit."""
    base = _CountingTransport()
    client = stub_client(
        transport=base,
        cache=HttpCache(policy=series_policy(), storage=storage),
    )

    r1 = await client.get("/series?id=A", expect=RESPONSE)
    assert base.count == 1
    assert r1.extensions.get("hishel_from_cache") is not True

    r2 = await client.get("/series?id=A", expect=RESPONSE)
    assert base.count == 1
    assert r2.extensions.get("hishel_from_cache") is True


async def test_cache_miss_distinct_url_refetches(storage: AsyncSqliteStorage) -> None:
    """Distinct query params are distinct cache keys, so each reaches the network."""
    base = _CountingTransport()
    client = stub_client(
        transport=base,
        cache=HttpCache(policy=series_policy(), storage=storage),
    )

    urls = ("/series?id=A", "/series?id=B")
    for url in urls:
        await client.get(url, expect=RESPONSE)

    assert base.count == len(urls)


async def test_the_client_ttl_is_threaded_and_a_per_call_cache_ttl_wins(storage: AsyncSqliteStorage) -> None:
    """A client whose ``HttpCache`` has a ``ttl`` stamps it as ``hishel_ttl`` on every request.

    The ``FilterPolicy``, not the TTL, decides what is stored, and a per-call ``cache_ttl=`` wins over the client's.
    """
    base = _CountingTransport()
    client = stub_client(
        transport=base,
        cache=HttpCache(policy=series_policy(), storage=storage, ttl=_CLIENT_TTL),
    )
    # Read off the network transport, so this checks what the wire sees however the client drives httpx2.
    captured = base.extensions

    await client.get("/series?id=A", expect=RESPONSE)
    assert captured[-1].get("hishel_ttl") == _CLIENT_TTL

    # An unmatched path still carries the TTL: the FilterPolicy, not a missing TTL, leaves it uncached.
    await client.get("/other/path", expect=RESPONSE)
    assert captured[-1].get("hishel_ttl") == _CLIENT_TTL

    await client.get("/series?id=C", expect=RESPONSE, cache_ttl=_CALL_TTL)
    assert captured[-1].get("hishel_ttl") == _CALL_TTL


async def _network_hits_for_two_gets(
    storage: AsyncSqliteStorage, *, client_ttl: float | None, call_ttl: float | None, gap: float
) -> int:
    """Count how many of two GETs of one cacheable URL, *gap* seconds apart, reached the network.

    The clock hishel reads (``time.time``) is frozen and then shifted by *gap*, so no test sleeps. hishel's storage
    applies the TTL, comparing an entry's ``created_at`` plus the ``hishel_ttl`` its stored request carried with that
    clock, so a TTL hidden from hishel (but still delivered to the network transport) would expire nothing.
    """
    base = _CountingTransport()
    client = stub_client(
        transport=base,
        cache=HttpCache(policy=series_policy(), storage=storage, ttl=client_ttl),
    )
    with time_machine.travel(datetime.now(UTC), tick=False) as clock:
        await client.get("/series?id=A", expect=RESPONSE, cache_ttl=call_ttl)
        clock.shift(gap)
        await client.get("/series?id=A", expect=RESPONSE, cache_ttl=call_ttl)
    return base.count


async def test_a_short_client_ttl_expires_the_entry(storage: AsyncSqliteStorage) -> None:
    """Hishel applies the client's TTL: past it, the entry is gone and the second GET goes back to the network."""
    hits = await _network_hits_for_two_gets(storage, client_ttl=_SHORT_TTL, call_ttl=None, gap=_GAP)

    assert hits == len(("first", "second"))


async def test_a_long_client_ttl_keeps_the_entry(storage: AsyncSqliteStorage) -> None:
    """Control for the test above: the same gap inside a long TTL is a hit, so the expiry above is the TTL's."""
    hits = await _network_hits_for_two_gets(storage, client_ttl=_CLIENT_TTL, call_ttl=None, gap=_GAP)

    assert hits == 1


async def test_a_per_call_cache_ttl_beats_the_client_ttl_inside_hishel(storage: AsyncSqliteStorage) -> None:
    """The per-call TTL wins where it matters, in the cache's expiry and not only on the request at the wire.

    It is on both calls because hishel judges an entry by the TTL its stored request carried: the first call's.
    """
    hits = await _network_hits_for_two_gets(storage, client_ttl=_CLIENT_TTL, call_ttl=_SHORT_TTL, gap=_GAP)

    assert hits == len(("first", "second"))


# A per-call cache_ttl= is forwarded by every verb that accepts one.

#: The client's HTTP verbs, every one of which takes a per-call ``cache_ttl``.
_VERBS = ("delete", "get", "head", "patch", "post", "put")


@pytest.mark.parametrize("verb", _VERBS)
def test_every_http_verb_takes_a_cache_ttl(verb: str) -> None:
    """Accept ``cache_ttl`` on every verb, so the forwarding test below covers them all."""
    assert "cache_ttl" in inspect.signature(getattr(AsyncBaseClient, verb)).parameters


@pytest.mark.parametrize("verb", _VERBS)
async def test_every_verb_forwards_a_per_call_cache_ttl(verb: str) -> None:
    """``cache_ttl=`` reaches the request the network sees (as ``hishel_ttl``) from every verb."""
    base = _CountingTransport()
    client = stub_client(transport=base)

    await getattr(client, verb)("/thing", expect=RESPONSE, cache_ttl=_CALL_TTL)

    assert base.extensions[-1].get("hishel_ttl") == _CALL_TTL


# A shared storage serves every client on it, and outlives them.


async def test_clients_share_one_storage_and_closing_them_never_closes_it() -> None:
    """A response one client stores is a hit for another on the same storage, and closing them all leaves it open.

    hishel's ``AsyncCacheTransport.aclose()`` closes its storage; ``ResilientCacheTransport.aclose()`` does not, or the
    first client's ``aclose()`` would close the storage under the rest. Whoever created the storage closes it.
    """
    shared = CloseCountingStorage(connection=await anysqlite.connect(":memory:"))
    network = _CountingTransport()
    try:
        clients = [
            stub_client(
                rate=Rate.per_second(2),
                transport=network,
                cache=HttpCache(policy=series_policy(), storage=shared),
            )
            for _ in range(_SHARING_CLIENTS)
        ]
        for client in clients:
            await client.get("/series?id=A", expect=RESPONSE)
        assert network.count == 1, "the first client stored it, and every other client hit it"

        for client in clients:
            await client.aclose()
        assert shared.closes == 0
        assert await shared.get_entries("nonexistent-key") == [], "the storage is still usable"
    finally:
        await shared.close()
    assert shared.closes == 1
