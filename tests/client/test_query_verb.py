# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""``Client.query``: the QUERY method, safe and idempotent, with a body like a POST's.

QUERY is one of the idempotent methods, so a QUERY is re-sent after any retryable failure, even with its body
written, and may be hedged. It is not cached.
"""

from __future__ import annotations

import asyncio

import httpx2

from headgate import Client, HttpOptions, Json, RetryPolicy, TransportPolicy, nothing
from headgate.retry import Backoff, Hedge
from headgate.testing import FakeTransport, TestClock

_BASE = "https://api.example.test/v1"
_QUERY = Json({"select": ["id"], "where": {"open": True}})
_SENT = b'{"select":["id"],"where":{"open":true}}'


def _client(fake: FakeTransport, *, retry: RetryPolicy | None = None) -> Client:
    """Return a client over *fake*, retrying under *retry*."""
    return Client(base_url=_BASE, policy=TransportPolicy(retry=retry), http=HttpOptions(transport=fake))


async def test_query_sends_its_body_with_the_query_method() -> None:
    """Send a QUERY carrying the encoded body, and read the answer through ``expect=``."""
    fake = FakeTransport().reply("QUERY", "/v1/orders", 200)

    await _client(fake).query("orders", body=_QUERY, expect=nothing)

    (sent,) = fake.calls
    assert (sent.method, sent.content, sent.headers["content-type"]) == ("QUERY", _SENT, "application/json")


async def test_a_query_without_a_body_sends_none() -> None:
    """Send a QUERY with no body when ``body=`` is left out."""
    fake = FakeTransport().reply("QUERY", "/v1/orders", 200)

    await _client(fake).query("orders", expect=nothing)

    (sent,) = fake.calls
    assert sent.content == b""


async def test_a_query_is_retried_with_its_whole_body_after_a_503() -> None:
    """Re-send a QUERY after a 503 as an idempotent request is, sending its body whole again."""
    fake = FakeTransport().reply("QUERY", "/v1/orders", 503, 200)
    retry = RetryPolicy(attempts=2, backoff=Backoff(factor=0.0))

    await _client(fake, retry=retry).query("orders", body=_QUERY, expect=nothing)

    assert [sent.content for sent in fake.calls] == [_SENT, _SENT]


async def test_a_slow_query_is_hedged() -> None:
    """Send a slow QUERY again beside the first, as a hedge may for any idempotent method."""
    clock = TestClock()
    first_sent = asyncio.Event()

    async def hangs(_request: httpx2.Request) -> httpx2.Response:
        first_sent.set()
        await asyncio.Event().wait()
        return httpx2.Response(200)  # never reached: the hedge answers first

    fake = FakeTransport().reply("QUERY", "/v1/orders", hangs, 200)
    with clock.install():
        client = _client(fake, retry=RetryPolicy(hedge=Hedge(after=0.5), budget=None))

    await client.query("orders", body=_QUERY, expect=nothing)

    assert [sent.content for sent in fake.calls] == [_SENT, _SENT]
