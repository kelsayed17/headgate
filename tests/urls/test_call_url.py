# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""A call's URL is a literal, a t-string or an absolute ``httpx2.URL``: the last is a wire URL, sent as it is.

A relative ``httpx2.URL`` is refused, since it would only skip escaping: ``httpx2.URL(f"items/{uid}")`` resolves
``../admin`` as a dot segment. A URL of any other kind is refused too, rather than sent as its text.
"""

from __future__ import annotations

from typing import Any, cast

import httpx2
import pytest

from headgate import Auth, BearerAuth, Client, HttpOptions, Rate, TransportPolicy, nothing
from headgate.events import CallFinished
from tests.support.events import Recorder

_BASE = "https://api.test/"


def _client(sent: list[httpx2.Request], recorder: Recorder, *, auth: Auth | None = None) -> Client:
    """Return a client on ``_BASE`` that records each request in *sent*."""

    def handle(request: httpx2.Request) -> httpx2.Response:
        sent.append(request)
        return httpx2.Response(httpx2.codes.OK, request=request)

    return Client(
        base_url=_BASE,
        rate=Rate.per_second(1000),
        auth=auth,
        policy=TransportPolicy(retry=None),
        http=HttpOptions(transport=httpx2.MockTransport(handle)),
        observers=(recorder,),
    )


async def test_a_url_object_is_sent_verbatim() -> None:
    """Send an ``httpx2.URL`` as it is, escapes and query included, and report no route for it."""
    sent: list[httpx2.Request] = []
    recorder = Recorder()
    async with _client(sent, recorder) as client:
        await client.get(httpx2.URL("https://api.test/items/a%2Fb?x=1"), expect=nothing)

    assert sent[0].url.raw_path == b"/items/a%2Fb?x=1"
    (finished,) = recorder.of(CallFinished)
    assert finished.call.route is None


async def test_an_absolute_url_object_is_sent_as_given() -> None:
    """Send an absolute ``httpx2.URL`` to its own origin, withholding the client's credential there."""
    sent: list[httpx2.Request] = []
    async with _client(sent, Recorder(), auth=BearerAuth("secret")) as client:
        await client.get(httpx2.URL("https://other.test/v2/x"), expect=nothing)
        await client.get("x", expect=nothing)

    foreign, home = sent
    assert str(foreign.url) == "https://other.test/v2/x"
    assert "Authorization" not in foreign.headers
    assert home.headers["Authorization"] == "Bearer secret"


async def test_a_t_string_is_still_escaped() -> None:
    """Escape a t-string's interpolation as one path segment."""
    sent: list[httpx2.Request] = []
    async with _client(sent, Recorder()) as client:
        await client.get(t"items/{'a/b'}", expect=nothing)

    assert sent[0].url.raw_path == b"/items/a%2Fb"


async def test_a_relative_url_object_is_refused() -> None:
    """Refuse a relative ``httpx2.URL`` before anything is sent, pointing to the t-string that escapes."""
    sent: list[httpx2.Request] = []
    async with _client(sent, Recorder(), auth=BearerAuth("secret")) as client:
        with pytest.raises(TypeError, match=r"a relative httpx2\.URL skips escaping: write a t-string") as raised:
            await client.get(httpx2.URL("items/../admin"), expect=nothing)

    assert "'items/../admin'" in str(raised.value)
    assert sent == []


@pytest.mark.parametrize("url", [None, b"items", ["items"], 3], ids=["none", "bytes", "list", "int"])
async def test_a_url_of_another_kind_is_refused(url: object) -> None:
    """Refuse a URL that is no ``str``, t-string or ``httpx2.URL``, rather than send its text with the credentials."""
    sent: list[httpx2.Request] = []
    async with _client(sent, Recorder(), auth=BearerAuth("secret")) as client:
        with pytest.raises(TypeError, match=r"^url= takes a literal str, a t-string or an absolute httpx2\.URL"):
            await client.get(cast("Any", url), expect=nothing)

    assert sent == []
