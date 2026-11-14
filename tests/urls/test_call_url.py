# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""A call's URL is a literal, a t-string or an ``httpx2.URL``: the last is a wire URL, sent as it is."""

from __future__ import annotations

import httpx2

from headgate import BearerAuth, Client, HttpOptions, Rate, TransportPolicy, nothing
from headgate.events import CallFinished
from tests.support.events import Recorder

_BASE = "https://api.test/"


def _client(sent: list[httpx2.Request], recorder: Recorder, *, auth: httpx2.Auth | None = None) -> Client:
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
        await client.get(httpx2.URL("items/a%2Fb?x=1"), expect=nothing)

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
