# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""Every verb, ``stream`` and ``paginate`` thread a call's own ``retry=`` and ``follow_redirects=`` to its send.

A scripted network answers a retryable 503 then a 200, or a redirect, so each option's effect shows in what was sent
and what the call returned or raised. A POST or PATCH carries an idempotency key, so the policy may re-send it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

import httpx2
import pytest

from headgate import HttpOptions, Rate, TransportPolicy, UpstreamStatusError, nothing
from headgate.pagination import NextUrl
from tests.support.clients import StubClient
from tests.support.wire import Script

if TYPE_CHECKING:
    from headgate import Method

_BASE: Final = "https://api.example.test"
_PATH: Final = "orders/1"
_KEY: Final = "key-1"
_VERBS: Final[tuple[Method, ...]] = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD")
#: A 503 the default policy re-sends, then the answer.
_FLAKY: Final = (503, 200)


def _no_items(_page: None) -> tuple[()]:
    """Return no items: the pages here carry none."""
    return ()


def _no_next(_page: None) -> None:
    """Name no next page."""


_ONE_PAGE: Final = NextUrl[None, object](items=_no_items, next=_no_next)


def _client(script: Script) -> StubClient:
    """Return a client over *script* under the default retry policy and no breaker."""
    return StubClient(
        base_url=_BASE,
        rate=Rate.per_second(1000),
        policy=TransportPolicy(breaker=None),
        http=HttpOptions(transport=script.transport()),
    )


def _moved() -> Script:
    """Return a network that redirects every request."""
    return Script(httpx2.Response(302, headers={"Location": f"{_BASE}/elsewhere"}))


async def _by_default(client: StubClient, method: Method) -> None:
    """Send *method* to :data:`_PATH` with the call's defaults."""
    match method:
        case "GET":
            await client.get(_PATH, expect=nothing)
        case "POST":
            await client.post(_PATH, expect=nothing, idempotency_key=_KEY)
        case "PUT":
            await client.put(_PATH, expect=nothing)
        case "PATCH":
            await client.patch(_PATH, expect=nothing, idempotency_key=_KEY)
        case "DELETE":
            await client.delete(_PATH, expect=nothing)
        case _:
            await client.head(_PATH, expect=nothing)


async def _without_retry(client: StubClient, method: Method) -> None:
    """Send *method* to :data:`_PATH` with ``retry=False``."""
    match method:
        case "GET":
            await client.get(_PATH, expect=nothing, retry=False)
        case "POST":
            await client.post(_PATH, expect=nothing, idempotency_key=_KEY, retry=False)
        case "PUT":
            await client.put(_PATH, expect=nothing, retry=False)
        case "PATCH":
            await client.patch(_PATH, expect=nothing, idempotency_key=_KEY, retry=False)
        case "DELETE":
            await client.delete(_PATH, expect=nothing, retry=False)
        case _:
            await client.head(_PATH, expect=nothing, retry=False)


async def _not_following(client: StubClient, method: Method) -> None:
    """Send *method* to :data:`_PATH` with ``follow_redirects=False``."""
    match method:
        case "GET":
            await client.get(_PATH, expect=nothing, follow_redirects=False)
        case "POST":
            await client.post(_PATH, expect=nothing, follow_redirects=False)
        case "PUT":
            await client.put(_PATH, expect=nothing, follow_redirects=False)
        case "PATCH":
            await client.patch(_PATH, expect=nothing, follow_redirects=False)
        case "DELETE":
            await client.delete(_PATH, expect=nothing, follow_redirects=False)
        case _:
            await client.head(_PATH, expect=nothing, follow_redirects=False)


@pytest.mark.parametrize("method", _VERBS)
async def test_every_verb_retries_by_default(method: Method) -> None:
    """Re-send a 503 under the client's policy when the call says nothing about retries."""
    script = Script(*_FLAKY)

    await _by_default(_client(script), method)

    assert [request.method for request in script.sent] == [method, method]


@pytest.mark.parametrize("method", _VERBS)
async def test_every_verb_sends_once_with_retry_false(method: Method) -> None:
    """Send once with ``retry=False``, raising the 503 with the call's own method."""
    script = Script(*_FLAKY)

    with pytest.raises(UpstreamStatusError) as raised:
        await _without_retry(_client(script), method)

    assert len(script.sent) == 1
    assert (raised.value.method, raised.value.status_code) == (method, 503)


@pytest.mark.parametrize("method", _VERBS)
async def test_every_verb_raises_a_redirect_it_does_not_follow(method: Method) -> None:
    """Raise the 302 itself with ``follow_redirects=False``, sending nothing to where it points."""
    script = _moved()

    with pytest.raises(UpstreamStatusError) as raised:
        await _not_following(_client(script), method)

    assert len(script.sent) == 1
    assert raised.value.status_code == httpx2.codes.FOUND


async def test_a_stream_sends_once_with_retry_false() -> None:
    """Open a stream with ``retry=False`` with one send, raising the 503."""
    script = Script(*_FLAKY)

    with pytest.raises(UpstreamStatusError):
        async with _client(script).stream("events", retry=False):
            pass

    assert len(script.sent) == 1


async def test_a_streams_last_event_id_joins_the_calls_own_headers() -> None:
    """Send ``Last-Event-ID`` beside the headers the call passed, not instead of them."""
    script = Script()

    async with _client(script).stream("events", headers={"X-Trace": "t-1"}, last_event_id="41"):
        pass

    sent = script.sent[0].headers
    assert (sent["x-trace"], sent["last-event-id"]) == ("t-1", "41")


async def test_paginate_refuses_a_request_body_with_its_reason() -> None:
    """Refuse a body option no pager can rewrite on ``paginate``, naming the ones a walk takes."""
    message = r"^paginate takes no data=, content= or files=: a walk of POSTs sends body= or json=$"
    with pytest.raises(ValueError, match=message):
        _client(Script()).paginate("orders", expect=nothing, pages=_ONE_PAGE, data={"a": 1})  # ty: ignore[no-matching-overload]


async def test_paginate_sends_each_page_once_with_retry_false() -> None:
    """Fetch a page with one send under ``retry=False``, raising the 503."""
    script = Script(*_FLAKY)
    pages = _client(script).paginate("orders", expect=nothing, pages=_ONE_PAGE, retry=False)

    with pytest.raises(UpstreamStatusError):
        await anext(aiter(pages.pages()))

    assert len(script.sent) == 1
