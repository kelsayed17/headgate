# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""A one-shot ``content=`` stream is sent once, and a factory's body whole on every send.

The client marks a one-shot body with ``REPLAYABLE_BODY``; the retry layer and the redirect hooks honour the mark.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx2
import pytest

from headgate import Expect, UpstreamStatusError, nothing
from headgate.testing import FakeTransport
from tests.integration._support import client

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from headgate import Method

_CHUNKS = (b"first,", b"second,", b"third")
_BODY = b"".join(_CHUNKS)


async def _chunks() -> AsyncIterator[bytes]:
    """Yield the body in pieces, as a one-shot stream does."""
    for chunk in _CHUNKS:
        yield chunk


@pytest.mark.parametrize("method", ["POST", "PUT"])
async def test_a_one_shot_body_answered_503_is_not_sent_again(method: Method) -> None:
    """Send a one-shot stream once, though the 503 is retryable, and raise it rather than ``StreamConsumed``."""
    fake = FakeTransport().reply(method, "/v1/upload", 503, 200)
    built = client(fake)

    with pytest.raises(UpstreamStatusError) as raised:
        async with built.stream("upload", method=method, content=_chunks(), idempotency_key="k"):
            pass

    assert raised.value.status_code == httpx2.codes.SERVICE_UNAVAILABLE
    assert [request.content for request in fake.calls] == [_BODY]
    assert "the request body is a one-shot stream, so it is not sent again" in raised.value.__notes__


@pytest.mark.parametrize("method", ["POST", "PUT"])
async def test_a_factory_body_is_sent_whole_on_every_attempt(method: Method) -> None:
    """Build the body afresh for each send, so a retried POST or PUT sends all of it again."""
    fake = FakeTransport().reply(method, "/v1/upload", 503, 200)
    built = client(fake)

    async with built.stream("upload", method=method, content=_chunks, idempotency_key="k") as stream:
        assert stream.status_code == httpx2.codes.OK

    assert [request.content for request in fake.calls] == [_BODY, _BODY]


@pytest.mark.parametrize("status", [307, 308])
async def test_a_redirect_asking_for_a_one_shot_body_again_is_not_followed(status: int) -> None:
    """Return the 307/308 as the final answer, with a note, instead of failing in httpx2 with ``StreamConsumed``."""
    moved = httpx2.Response(status, headers={"Location": "/v1/elsewhere"})
    fake = FakeTransport().reply("POST", "/v1/upload", moved).reply("POST", "/v1/elsewhere", 200)
    built = client(fake)

    with pytest.raises(UpstreamStatusError) as raised:
        await built.post("upload", expect=nothing, content=_chunks())

    assert raised.value.status_code == status
    assert len(fake.calls) == 1
    assert raised.value.__notes__[-1] == (
        f"did not follow the {status} redirect: it asks for the request body again, and the body was a one-shot stream "
        "(pass content= a factory to make it replayable)"
    )


async def test_a_call_not_following_redirects_keeps_the_location_of_a_one_shot_bodys_307() -> None:
    """Leave a 307 a call does not follow as it came, ``Location`` and all, with no note: nothing was refused."""
    moved = httpx2.Response(httpx2.codes.TEMPORARY_REDIRECT, headers={"Location": "/v1/elsewhere"})
    fake = FakeTransport().reply("POST", "/v1/upload", moved)
    built = client(fake)

    location = await built.post(
        "upload",
        expect=Expect(nothing).on(httpx2.codes.TEMPORARY_REDIRECT, _location),
        content=_chunks(),
        follow_redirects=False,
    )

    assert location == "/v1/elsewhere"


def _location(response: httpx2.Response, /) -> str | None:
    """Return *response*'s ``Location``, if it kept one."""
    return response.headers.get("Location")


async def test_a_declared_redirect_answer_reads_the_unfollowed_hop() -> None:
    """Hand a call that declares the 307 its response, ``Location`` removed, since it was not followed."""
    moved = httpx2.Response(httpx2.codes.TEMPORARY_REDIRECT, headers={"Location": "/v1/elsewhere"})
    fake = FakeTransport().reply("POST", "/v1/upload", moved)
    built = client(fake)

    status = await built.post(
        "upload", expect=Expect(nothing).on(httpx2.codes.TEMPORARY_REDIRECT, _status), content=_chunks()
    )

    assert status == httpx2.codes.TEMPORARY_REDIRECT


def _status(response: httpx2.Response, /) -> int:
    """Return *response*'s status."""
    return response.status_code


@pytest.mark.parametrize(("status", "method"), [(303, "PUT"), (302, "POST"), (301, "POST")])
async def test_a_redirect_that_drops_the_body_is_followed(status: int, method: Method) -> None:
    """Follow a redirect httpx2 turns into a body-less GET, since nothing is sent twice."""
    moved = httpx2.Response(status, headers={"Location": "/v1/done"})
    fake = FakeTransport().reply(method, "/v1/upload", moved).reply("GET", "/v1/done", 200)
    built = client(fake)

    async with built.stream("upload", method=method, content=_chunks()) as stream:
        assert stream.status_code == httpx2.codes.OK

    assert [request.method for request in fake.calls] == [method, "GET"]


async def test_a_factory_body_follows_a_307() -> None:
    """Follow a 307 with a factory body, sending the whole body to the new location."""
    moved = httpx2.Response(httpx2.codes.TEMPORARY_REDIRECT, headers={"Location": "/v1/elsewhere"})
    fake = FakeTransport().reply("POST", "/v1/upload", moved).reply("POST", "/v1/elsewhere", 200)
    built = client(fake)

    await built.post("upload", expect=nothing, content=_chunks)

    assert [request.content for request in fake.calls] == [_BODY, _BODY]
