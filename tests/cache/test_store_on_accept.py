# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""A miss is stored only once the call's reader accepts the response.

Many providers report a refusal inside a 200 (a spent quota, a bad key), which a reader's ``refusal_check`` turns into
an error. Stored on the way through, that refusal would be served for the entry's whole lifetime without the provider
being asked again. Stored on accept, a response any reader refuses is never stored, while one it accepts is, as before.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, override

import httpx2
import pytest

from headgate import HttpCache, Reader, RefusedError, as_bytes
from headgate.cache import Partition
from headgate.cache._transport import ResilientCacheTransport
from headgate.errors import ClientError
from tests.cache._helpers import GENEROUS_CAP, FaultyStorage, accept, cache_transport, every_get_rules, stub_client

if TYPE_CHECKING:
    from hishel import AsyncSqliteStorage, Entry, Request, Response

_REFUSED = b"quota exhausted"
_ANSWER = b"42"


class _Provider(httpx2.AsyncBaseTransport):
    """Answer the first request with *first*, a 200 by default, and every later one with a 200 carrying the answer."""

    def __init__(self, first: bytes = _REFUSED, *, status: int = httpx2.codes.OK) -> None:
        """Answer the first request with *status* and *first*."""
        self.sent = 0
        self._first = first
        self._status = status

    @override
    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        self.sent += 1
        if self.sent == 1:
            return httpx2.Response(self._status, content=self._first)
        return httpx2.Response(httpx2.codes.OK, content=_ANSWER)


@Reader
def _refused_in_200(resp: httpx2.Response, /) -> bytes:
    """Read the body, refusing the provider's in-band quota error."""
    if resp.content == _REFUSED:
        msg = "refused: quota exhausted"
        raise RefusedError(msg, code="quota")
    return resp.content


@Reader
def _numeric(resp: httpx2.Response, /) -> int:
    """Read the body as a number, which a refusal is not."""
    return int(resp.content)


@pytest.mark.parametrize("read", [_refused_in_200, _numeric], ids=["a-refusal-in-a-200", "an-unreadable-body"])
async def test_a_response_the_reader_refuses_is_never_stored(read: Reader[object], storage: AsyncSqliteStorage) -> None:
    """Refuse the first response, then ask the provider again, and store the answer the reader accepts."""
    network = _Provider()
    client = stub_client(transport=network, cache=HttpCache(**every_get_rules(), storage=storage, ttl=3600))
    try:
        with pytest.raises(ClientError):
            await client.get("reading", expect=read)
        second = await client.get("reading", expect=read)
        third = await client.get("reading", expect=read)
    finally:
        await client.aclose()

    assert second == third == read(httpx2.Response(httpx2.codes.OK, content=_ANSWER))
    assert network.sent == len(("the refused one", "the accepted one")), "the third call was a hit"


@pytest.mark.parametrize(("statuses", "second"), [({203}, b"partial"), ({200}, _ANSWER)], ids=["named", "not-named"])
async def test_only_a_status_the_cache_names_is_stored(
    storage: AsyncSqliteStorage, statuses: set[int], second: bytes
) -> None:
    """Store a 203 the cache's ``statuses`` names, and send again for one it does not, though hishel would store it."""
    network = _Provider(b"partial", status=httpx2.codes.NON_AUTHORITATIVE_INFORMATION)
    client = stub_client(transport=network, cache=HttpCache(storage=storage, statuses=statuses))
    try:
        answers = [await client.get("reading", expect=as_bytes) for _ in range(2)]
    finally:
        await client.aclose()

    assert answers == [b"partial", second], "the second call was a hit only when the 203 was stored"


async def test_a_stream_is_stored_once_its_head_is_accepted(storage: AsyncSqliteStorage) -> None:
    """Store a stream's response when it opens: a stream has no reader, so passing the head checks accepts it."""
    network = _Provider(_ANSWER)
    client = stub_client(transport=network, cache=HttpCache(**every_get_rules(), storage=storage))
    try:
        async with client.stream("reading") as stream:
            assert [chunk async for chunk in stream.aiter_bytes()] == [_ANSWER]
        assert await client.get("reading", expect=as_bytes) == _ANSWER
    finally:
        await client.aclose()

    assert network.sent == 1


class _CountingStorage(FaultyStorage):
    """A storage that counts the entries written."""

    created = 0

    @override
    async def create_entry(
        self, request: Request, response: Response, key: str, *, ttl: float | None = None, tti: float | None = None
    ) -> Entry:
        self.created += 1
        return await super().create_entry(request, response, key, ttl=ttl, tti=tti)


async def test_a_response_accepted_twice_is_stored_once(storage: AsyncSqliteStorage) -> None:
    """Write a miss once, however many copies of it are accepted (a shared download's waiting requests, say)."""
    counting = _CountingStorage(storage)
    response = await cache_transport(counting, _Provider(_ANSWER)).handle_async_request(
        httpx2.Request("GET", "https://x/reading")
    )

    await accept(response)
    await accept(response)

    assert counting.created == 1


class _Varying(httpx2.AsyncBaseTransport):
    """Answer every request with a 200 whose ``Vary`` is *vary*."""

    def __init__(self, vary: str) -> None:
        """Answer with ``Vary: <vary>``."""
        self._vary = vary

    @override
    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(httpx2.codes.OK, headers={"Vary": self._vary}, content=_ANSWER)


@pytest.mark.parametrize(
    ("vary", "partition", "sent", "created"),
    [
        ("*", Partition.CREDENTIAL, {}, 0),
        ("Accept, Authorization", Partition.SHARED, {"Authorization": "Bearer alice"}, 0),
        ("Accept, Authorization", Partition.CREDENTIAL, {"Authorization": "Bearer alice"}, 1),
        ("Accept, Authorization", Partition.SHARED, {}, 1),
        ("Accept-Language", Partition.SHARED, {"Accept-Language": "en"}, 1),
    ],
    ids=["star", "shared-dropped", "credential-dropped", "shared-absent", "shared-kept"],
)
async def test_a_variant_no_later_request_could_be_matched_to_is_never_stored(
    storage: AsyncSqliteStorage, vary: str, partition: Partition, sent: dict[str, str], created: int
) -> None:
    """Write nothing for ``Vary: *``, or for a shared entry varying on a header the view drops, such as a credential.

    Under a credential partition the dropped header's value is in the partition label, so each value has its own
    entries; a header the request did not send, or one the view keeps as a digest, is matched as usual.
    """
    counting = _CountingStorage(storage)
    cache = HttpCache(storage=counting, partition=partition)
    transport = ResilientCacheTransport(_Varying(vary), cache, max_body_bytes=GENEROUS_CAP)

    await accept(await transport.handle_async_request(httpx2.Request("GET", "https://x/reading", headers=sent)))

    assert counting.created == created


async def test_a_miss_reports_itself_not_yet_stored(storage: AsyncSqliteStorage) -> None:
    """Report ``stored`` false on a miss, which is written only once its call accepts it, whatever hishel said."""
    response = await cache_transport(storage, _Provider(_ANSWER)).handle_async_request(
        httpx2.Request("GET", "https://x/reading")
    )
    await response.aread()

    assert response.extensions["caching"] == {"from_cache": False, "revalidated": False, "stored": False}


async def test_a_response_never_accepted_is_never_stored(storage: AsyncSqliteStorage) -> None:
    """Write nothing for a miss nobody accepted, even one the policy's filters allow."""
    counting = _CountingStorage(storage)
    response = await cache_transport(counting, _Provider(_ANSWER)).handle_async_request(
        httpx2.Request("GET", "https://x/reading")
    )

    assert await response.aread() == _ANSWER
    assert counting.created == 0
