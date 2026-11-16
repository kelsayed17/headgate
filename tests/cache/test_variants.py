# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""Which stored entry answers which request: a different ``Accept``, a ``Range``, a credential, a hedge.

An entry answers a request only if it is the response that request asked for. Two requests for one URL with different
``Accept`` headers ask for different representations, and a ``Range`` asks for part of one, so neither is answered by
an entry stored for another. Under a shared partition, a request carrying a credential is never answered by an entry
that varies on it. And a hedge of a cached GET still reaches the network beside its first send.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Final, override

import httpx2

from headgate import HttpCache, RetryPolicy, as_bytes
from headgate.cache import Partition
from headgate.cache._transport import served_from_cache
from headgate.retry import Hedge
from tests.cache._helpers import stub_client
from tests.support.responses import RESPONSE

if TYPE_CHECKING:
    from hishel import AsyncSqliteStorage

_PAIR: Final = 2


class _Echo(httpx2.AsyncBaseTransport):
    """The network leg: counts sends, and answers with the request's ``Accept``, ``Range`` and ``Authorization``.

    It answers a ``Range`` with a 206, and adds *vary* to every response's ``Vary``.
    """

    def __init__(self, vary: str | None = None) -> None:
        self.sent = 0
        self._vary = vary

    @override
    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        self.sent += 1
        names = ("accept", "range", "authorization")
        body = ";".join(f"{name}={request.headers.get(name, '')}" for name in names).encode()
        headers = {} if self._vary is None else {"Vary": self._vary}
        status = httpx2.codes.PARTIAL_CONTENT if "range" in request.headers else httpx2.codes.OK
        return httpx2.Response(status, headers=headers, content=body)


# Audit item 6: Accept and Range.


async def test_a_different_accept_has_an_entry_of_its_own(storage: AsyncSqliteStorage) -> None:
    """Fetch a CSV for a call that accepts one, though the JSON for the same URL is stored."""
    network = _Echo()
    client = stub_client(transport=network, cache=HttpCache(storage=storage))
    async with client:
        json = await client.get("report", expect=as_bytes, headers={"Accept": "application/json"})
        csv = await client.get("report", expect=as_bytes, headers={"Accept": "text/csv"})
        again = await client.get("report", expect=RESPONSE, headers={"Accept": "text/csv"})

    assert json.startswith(b"accept=application/json;")
    assert csv.startswith(b"accept=text/csv;")
    assert network.sent == _PAIR
    assert served_from_cache(again), "each representation has its own entry"


async def test_a_range_request_is_never_answered_from_the_cache(storage: AsyncSqliteStorage) -> None:
    """Send a ``Range`` request to the network though the whole body is stored, and store nothing of its answer."""
    network = _Echo()
    client = stub_client(transport=network, cache=HttpCache(storage=storage, statuses={200}))
    async with client:
        await client.get("report", expect=as_bytes)
        part = await client.get("report", expect=RESPONSE, headers={"Range": "bytes=0-3"})

    assert part.status_code == httpx2.codes.PARTIAL_CONTENT
    assert b"range=bytes=0-3" in part.content
    assert network.sent == _PAIR


# Audit item 7: a shared partition and Vary: Authorization on lookup.


async def test_a_shared_entry_varying_on_a_credential_never_answers_one_that_sends_it(
    storage: AsyncSqliteStorage,
) -> None:
    """Fetch an authenticated call's own answer, though an anonymous one for the same URL varying on it is stored."""
    network = _Echo(vary="Authorization")
    client = stub_client(transport=network, cache=HttpCache(storage=storage, partition=Partition.SHARED))
    async with client:
        anonymous = await client.get("report", expect=as_bytes)
        mine = await client.get("report", expect=RESPONSE, headers={"Authorization": "Bearer alice"})

    assert anonymous.endswith(b"authorization=")
    assert mine.content.endswith(b"authorization=Bearer alice")
    assert not served_from_cache(mine)


# Audit item 8: a hedge of a cached GET.


class _FirstHangs(httpx2.AsyncBaseTransport):
    """The network leg: the first send never answers, and every later one answers at once."""

    def __init__(self) -> None:
        self.sent = 0

    @override
    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        self.sent += 1
        if self.sent == 1:
            await asyncio.Event().wait()
        return httpx2.Response(httpx2.codes.OK, content=b"hedged")


async def test_a_hedge_of_a_cached_get_answers_the_call(storage: AsyncSqliteStorage) -> None:
    """Answer a cached GET from its hedge when its first send never answers."""
    network = _FirstHangs()
    client = stub_client(transport=network, cache=HttpCache(storage=storage))
    hedged = RetryPolicy(hedge=Hedge(after=0.05), budget=None)
    async with client, asyncio.timeout(5):
        body = await client.get("report", expect=as_bytes, retry=hedged)

    assert body == b"hedged"
    assert network.sent == _PAIR
