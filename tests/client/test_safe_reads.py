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

from headgate import Client, HttpOptions, Json, nothing
from headgate._core import CallContext, CallInfo
from headgate.cache._transport import HAS_BODY, served_from_cache
from headgate.testing import FakeTransport, Respond
from tests.cache._helpers import Network, accept, cache_transport

_URL = "https://x/series"

if TYPE_CHECKING:
    from pathlib import Path

    from hishel import AsyncSqliteStorage

    from headgate import Method


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


@pytest.mark.parametrize("method", ["GET", "HEAD"])
async def test_a_get_or_head_stream_refuses_a_body(method: Method) -> None:
    """Refuse a body on a GET or HEAD stream before anything is sent, pointing to ``method="QUERY"``."""
    fake = FakeTransport()
    async with Client(base_url="https://api.example", http=HttpOptions(transport=fake)) as client:
        with pytest.raises(TypeError, match=rf"^stream\(\) takes no body= for a {method}: .*method='QUERY'"):
            async with client.stream("x", method=method, body=Json({"q": "alice"})):
                pass

    assert fake.calls == ()


async def test_an_event_stream_refuses_a_body() -> None:
    """Refuse a body on ``events()``, a GET stream, the same way."""
    fake = FakeTransport()
    async with Client(base_url="https://api.example", http=HttpOptions(transport=fake)) as client:
        with pytest.raises(TypeError, match=r"takes no body= for a GET: .*method='QUERY'"):
            async with client.events("feed", body=Json({"q": "alice"})):
                pass

    assert fake.calls == ()


async def test_a_query_stream_takes_a_body() -> None:
    """Send a QUERY stream's body: the safe read with a body."""
    fake = FakeTransport().reply("QUERY", "/x", Respond(content=b"ok"))
    client = Client(base_url="https://api.example", http=HttpOptions(transport=fake))
    async with client, client.stream("x", method="QUERY", body=Json({"q": "alice"})) as stream:
        assert b"".join([chunk async for chunk in stream.aiter_bytes()]) == b"ok"

    assert fake.calls[0].content == b'{"q":"alice"}'


async def test_the_cache_passes_a_call_given_a_body_by(storage: AsyncSqliteStorage) -> None:
    """Send a call marked as given a body to the network, though its URL's entry is stored and its headers say none.

    No verb or stream sends a GET with a body now; the mark is the cache's defense should one arrive. It is the call's
    own, so a ``Content-Length: 0`` the caller wrote does not hide the body.
    """
    network = Network()
    transport = cache_transport(storage, network)
    await accept(await transport.handle_async_request(httpx2.Request("GET", _URL)))
    bodied = httpx2.Request("GET", _URL, headers={"Content-Length": "0"})
    context = CallContext(CallInfo(method="GET", url=_URL))
    context.put(HAS_BODY, value=True)
    context.attach(bodied)

    await (await transport.handle_async_request(bodied)).aread()
    hit = await transport.handle_async_request(httpx2.Request("GET", _URL))

    assert network.calls == len(("stored", "bodied")), "the bodied call was answered from the cache"
    assert served_from_cache(hit)
