# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""``FakeTransport``: a route table of canned replies under a real client, with a log of every request."""

from __future__ import annotations

import gzip

import httpx2
import pytest

from headgate import HttpOptions, Rate, TransportPolicy, as_bytes, nothing
from headgate.errors import TransportError, UpstreamStatusError
from headgate.retry import NO_RETRY
from headgate.testing import FakeTransport, UnmatchedRequestError
from tests.support.clients import StubClient

_BASE = "https://api.example.test/v1"
_RATE = Rate.per_second(1000)


def _client(fake: FakeTransport, *, policy: TransportPolicy | None = None) -> StubClient:
    """Build a client over *fake*."""
    return StubClient(base_url=_BASE, rate=_RATE, policy=policy or TransportPolicy(), http=HttpOptions(transport=fake))


async def test_a_route_serves_its_replies_in_order_and_repeats_the_last() -> None:
    """Serve a retry sequence from one route, then keep serving its last reply."""
    fake = FakeTransport().reply("GET", "/v1/series", 503, httpx2.Response(200, content=b"ok"))
    client = _client(fake)

    assert await client.get("series", expect=as_bytes) == b"ok"
    assert await client.get("series", expect=as_bytes) == b"ok"
    assert [request.url.path for request in fake.calls] == ["/v1/series"] * len(("503", "200", "200"))


async def test_a_route_without_replies_answers_an_empty_200() -> None:
    """Answer with an empty 200 when a route was given no reply."""
    fake = FakeTransport().reply("delete", "/v1/orders/1")
    await _client(fake).delete("orders/1", expect=nothing)
    assert len(fake.calls_to("DELETE", "/v1/orders/1")) == 1


async def test_a_reply_function_sees_the_request_and_may_be_async() -> None:
    """Answer through a sync or an async function of the request, which sees its body."""

    def echo(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, content=request.content)

    async def shout(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, content=request.content.upper())

    fake = FakeTransport().reply("POST", "/v1/echo", echo).reply("POST", "/v1/shout", shout)
    client = _client(fake)

    assert await client.post("echo", expect=as_bytes, data={"a": "b"}) == b"a=b"
    assert await client.post("shout", expect=as_bytes, data={"a": "b"}) == b"A=B"


async def test_a_reply_function_can_fail_the_network() -> None:
    """Raise what the reply function raises, which the client maps like any network failure."""

    def refuse(request: httpx2.Request) -> httpx2.Response:
        msg = "connection refused"
        raise httpx2.ConnectError(msg, request=request)

    fake = FakeTransport().reply("GET", "/v1/x", refuse)
    with pytest.raises(TransportError):
        await _client(fake, policy=TransportPolicy(retry=NO_RETRY)).get("x", expect=nothing)


async def test_a_compressed_reply_is_served_again_and_again() -> None:
    """Copy a response per request, so an encoded body decodes every time."""
    compressed = httpx2.Response(200, content=gzip.compress(b"hello"), headers={"content-encoding": "gzip"})
    fake = FakeTransport().reply("GET", "/v1/file", compressed)
    client = _client(fake)
    assert await client.get("file", expect=as_bytes) == b"hello"
    assert await client.get("file", expect=as_bytes) == b"hello"


async def test_an_unmatched_request_fails_the_test_rather_than_the_call() -> None:
    """Raise ``UnmatchedRequestError`` naming the request (credentials redacted) and the routes, unretried."""
    fake = FakeTransport().reply("GET", "/v1/known")
    with pytest.raises(UnmatchedRequestError) as caught:
        await _client(fake).get("unknown", expect=nothing, params={"api_key": "s3cret"})

    message = str(caught.value)
    assert "GET https://api.example.test/v1/unknown?api_key=" in message
    assert "s3cret" not in message
    assert "routes: GET /v1/known" in message
    assert len(fake.calls) == 1


async def test_an_empty_table_says_it_has_no_routes() -> None:
    """Name the absence of routes when there are none."""
    with pytest.raises(UnmatchedRequestError, match=r"routes: none"):
        await _client(FakeTransport()).get("x", expect=nothing)


async def test_a_status_reply_reaches_the_status_ladder() -> None:
    """Serve a bare status as an empty response with it."""
    fake = FakeTransport().reply("GET", "/v1/missing", httpx2.codes.NOT_FOUND)
    with pytest.raises(UpstreamStatusError) as caught:
        await _client(fake).get("missing", expect=nothing)
    assert caught.value.status_code == httpx2.codes.NOT_FOUND


async def test_closing_the_client_closes_the_transport() -> None:
    """Record that the client closed it."""
    fake = FakeTransport()
    async with _client(fake):
        assert fake.closed is False
    assert fake.closed is True
