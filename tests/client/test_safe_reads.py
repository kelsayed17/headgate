# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""A GET or HEAD has no body, and the cache never answers a request that has one.

The cache keys an entry by its URL, so two GETs with different bodies would share one: the second would be served the
first's answer. ``get()`` and ``head()`` take no ``body=`` (``query()`` is the safe read with a body), and as defense in
depth the cache sends any request with a body straight to the network: never looked up, stored or shared.
"""

from __future__ import annotations

import asyncio
import sys
from textwrap import dedent
from typing import TYPE_CHECKING

import httpx2
import pytest

from headgate import Client, HttpCache, HttpOptions, Json, TransportPolicy, as_bytes, nothing
from headgate.testing import FakeTransport

if TYPE_CHECKING:
    from pathlib import Path

    from hishel import AsyncSqliteStorage


@pytest.mark.parametrize("verb", ["get", "head"])
@pytest.mark.parametrize("keyword", ["body", "json", "data", "content", "files"])
async def test_get_and_head_refuse_a_body(verb: str, keyword: str) -> None:
    """Refuse a body keyword on a GET or HEAD before anything is sent, pointing to ``query()``."""
    fake = FakeTransport()
    async with Client(base_url="https://api.example", http=HttpOptions(transport=fake)) as client:
        call = getattr(client, verb)
        with pytest.raises(
            TypeError, match=rf"^{verb}\(\) got an unexpected keyword argument '{keyword}': .*query\(\)"
        ):
            await call("x", expect=nothing, **{keyword: Json({"q": "alice"})})

    assert fake.calls == ()


async def test_a_body_on_get_does_not_type_check(tmp_path: Path) -> None:
    """Report ``body=`` on ``get`` and ``head`` as a type error, as on any other unknown keyword."""
    module = tmp_path / "probe.py"
    module.write_text(
        dedent(
            """
            from headgate import Client, Json, nothing


            async def probe(c: Client) -> None:
                await c.get("x", expect=nothing, body=Json({"q": "alice"}))
                await c.head("x", expect=nothing, body=Json({"q": "alice"}))
            """
        )
    )
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "ty", "check", str(module), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    stdout, _ = await process.communicate()

    out = stdout.decode()
    assert "Argument `body` does not match any known parameter of bound method `Client.get`" in out, out
    assert "Found 2 diagnostics" in out, out


async def _read(client: Client, body: Json | None) -> bytes:
    """Open a GET stream of ``search`` with *body*, and return what it streams."""
    async with client.stream("search", method="GET", body=body) as stream:
        return b"".join([chunk async for chunk in stream.aiter_bytes()])


def _echoing(storage: AsyncSqliteStorage, sent: list[bytes]) -> Client:
    """Return a client caching into *storage*, whose network records each request body and answers with it."""

    def handle(request: httpx2.Request) -> httpx2.Response:
        sent.append(request.read())
        return httpx2.Response(200, content=request.content or b"none")

    return Client(
        base_url="https://api.example",
        policy=TransportPolicy(cache=HttpCache(storage=storage, ttl=60)),
        http=HttpOptions(transport=httpx2.MockTransport(handle)),
    )


async def test_the_cache_passes_a_bodied_request_by(storage: AsyncSqliteStorage) -> None:
    """Send a bodied GET stream to the network, though the URL's entry is stored, so it gets its own body's answer."""
    sent: list[bytes] = []
    async with _echoing(storage, sent) as client:
        stored = await client.get("search", expect=as_bytes)
        answer = await _read(client, Json({"q": "bob"}))
        hit = await client.get("search", expect=as_bytes)

    assert (stored, answer, hit) == (b"none", b'{"q":"bob"}', b"none")
    assert sent == [b"", b'{"q":"bob"}'], "the bodied stream was answered from the cache"
