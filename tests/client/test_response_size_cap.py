"""Response-body size cap on the shared transport (``AsyncBaseClient``).

Every client buffers the whole response into memory before decoding, so an
unbounded body is an OOM vector. The cap has one non-obvious requirement:
it must count **decompressed** bytes.

``Content-Length`` is the *compressed* size, so a header check alone is
useless against a gzip bomb — ~500 KB on the wire expands to 524 MB. It is
also not always present: a filing index is served chunked with no
``Content-Length`` at all. And a compression-ratio heuristic would
false-positive on real data (an aggregate JSON legitimately
compresses 14x). Hence: stream, count decoded bytes, abort past the ceiling.
"""

from __future__ import annotations

import gzip
from typing import TYPE_CHECKING, override

import httpx2
import pytest

from headgate._body import DEFAULT_MAX_RESPONSE_BYTES
from headgate._client import AsyncBaseClient
from headgate._rate_limit import Rate
from headgate.errors import ClientError, ResponseTooLargeError
from tests.support.responses import RESPONSE

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from pytest_httpx2 import HTTPXMock


class _Client(AsyncBaseClient):
    def __init__(self, *, max_response_bytes: int, transport: httpx2.AsyncBaseTransport | None = None) -> None:
        super().__init__(
            base_url="https://example.test",
            rate=Rate.per_second(1000),
            max_response_bytes=max_response_bytes,
            transport=transport,
        )


class TestResponseSizeCap:
    async def test_body_under_cap_passes_through_intact(self, httpx2_mock: HTTPXMock) -> None:
        body = b"x" * 1024
        httpx2_mock.add_response(content=body)
        c = _Client(max_response_bytes=8192)
        try:
            resp = await c.get("thing", expect=RESPONSE)
            assert resp.content == body
            assert resp.status_code == 200
            assert resp.is_success
        finally:
            await c.aclose()

    async def test_body_over_cap_raises(self, httpx2_mock: HTTPXMock) -> None:
        httpx2_mock.add_response(content=b"x" * 20_000)
        c = _Client(max_response_bytes=8192)
        try:
            with pytest.raises(ResponseTooLargeError) as ei:
                await c.get("thing", expect=RESPONSE)
            assert isinstance(ei.value, ClientError)  # maps to 502 at the boundary
        finally:
            await c.aclose()

    async def test_gzip_bomb_is_capped_on_DECOMPRESSED_size(self) -> None:
        """The whole point: a tiny wire body that explodes when decoded.

        A ``Content-Length``-only check would wave this straight through —
        the compressed body is a few KB.

        Driven through ``httpx2.MockTransport`` with a real ``ByteStream``
        rather than ``httpx2_mock``: content-decoding only runs when the
        response has a *stream* (a response constructed with ``content=``
        sets ``_content`` and ``aiter_bytes`` skips the decoder entirely),
        and this test is worthless unless the gzip is really decoded.
        """
        payload = b"A" * (8 * 1024 * 1024)
        wire = gzip.compress(payload)
        assert len(wire) < 100_000, "fixture should be tiny on the wire"

        def handler(request: httpx2.Request) -> httpx2.Response:
            return httpx2.Response(
                200,
                headers={
                    "Content-Encoding": "gzip",
                    "Content-Length": str(len(wire)),
                },
                stream=httpx2.ByteStream(wire),
            )

        c = _Client(max_response_bytes=1024 * 1024, transport=httpx2.MockTransport(handler))
        try:
            with pytest.raises(ResponseTooLargeError):
                await c.get("bomb", expect=RESPONSE)
        finally:
            await c.aclose()

    async def test_httpx2_decodes_a_bomb_in_slices_of_at_most_one_mebibyte(self) -> None:
        """The dependency property ``read_capped`` relies on for PEAK memory, pinned so a regression shows.

        The cap bounds the ACCUMULATED body; the transient peak is the largest single decoded chunk, which httpx2
        (2.12+) bounds at 1 MiB by decompressing with ``max_length`` (``httpx2._decoders.MAX_DECODE_CHUNK_SIZE``).
        Under upstream httpx2 0.28 one network read of a 1028:1 bomb decoded into a single 33 MB chunk. Fed here in
        16 KiB reads, as a socket would deliver it: one ``ByteStream`` would hand the decoder the whole wire at once
        and say nothing about how a real stream is sliced.
        """
        wire = gzip.compress(b"\0" * (64 * 1024 * 1024), compresslevel=9)

        class _Socket(httpx2.AsyncByteStream):
            @override
            async def __aiter__(self) -> AsyncIterator[bytes]:
                for i in range(0, len(wire), 16 * 1024):
                    yield wire[i : i + 16 * 1024]

        def handler(request: httpx2.Request) -> httpx2.Response:
            return httpx2.Response(200, headers={"Content-Encoding": "gzip"}, stream=_Socket())

        biggest = total = 0
        async with (
            httpx2.AsyncClient(transport=httpx2.MockTransport(handler)) as http,
            http.stream("GET", "https://example.test/bomb") as resp,
        ):
            async for chunk in resp.aiter_bytes():
                biggest, total = max(biggest, len(chunk)), total + len(chunk)
                if total > 4 * 1024 * 1024:
                    break
        assert total > 4 * 1024 * 1024, "the bomb must actually decode for the bound to mean anything"
        assert biggest <= 1024 * 1024

    async def test_gzip_under_cap_decodes_intact(self) -> None:
        """The cap must not corrupt or truncate a legitimate compressed body."""
        payload = b"A" * (2 * 1024 * 1024)
        wire = gzip.compress(payload)

        def handler(request: httpx2.Request) -> httpx2.Response:
            return httpx2.Response(
                200,
                headers={"Content-Encoding": "gzip"},
                stream=httpx2.ByteStream(wire),
            )

        c = _Client(max_response_bytes=16 * 1024 * 1024, transport=httpx2.MockTransport(handler))
        try:
            resp = await c.get("ok", expect=RESPONSE)
            assert resp.content == payload
            # The re-attached body is decoded, so the wire-encoding headers
            # must not survive to misdescribe it.
            assert "content-encoding" not in resp.headers
        finally:
            await c.aclose()

    async def test_declared_content_length_rejects_before_transfer(self, httpx2_mock: HTTPXMock) -> None:
        """An honest oversized body is refused on the header, not downloaded."""
        httpx2_mock.add_response(content=b"x" * 100, headers={"Content-Length": "999999999"})
        c = _Client(max_response_bytes=8192)
        try:
            with pytest.raises(ResponseTooLargeError):
                await c.get("huge", expect=RESPONSE)
        finally:
            await c.aclose()

    async def test_missing_content_length_still_capped(self, httpx2_mock: HTTPXMock) -> None:
        """Chunked responses carry no Content-Length — the House FD index is one."""
        httpx2_mock.add_response(content=b"x" * 20_000, headers={"Transfer-Encoding": "chunked"})
        c = _Client(max_response_bytes=8192)
        try:
            with pytest.raises(ResponseTooLargeError):
                await c.get("chunked", expect=RESPONSE)
        finally:
            await c.aclose()

    async def test_default_clears_the_largest_real_payload(self) -> None:
        """3.3 MB (an aggregate ticker JSON) is the largest measured live body."""
        assert DEFAULT_MAX_RESPONSE_BYTES >= 8 * 1024 * 1024

    async def test_per_client_override_raises_the_ceiling(self, httpx2_mock: HTTPXMock) -> None:
        body = b"x" * 20_000
        httpx2_mock.add_response(content=body)
        c = _Client(max_response_bytes=64_000)
        try:
            assert (await c.get("thing", expect=RESPONSE)).content == body
        finally:
            await c.aclose()


async def test_a_followed_redirect_body_is_dropped_not_buffered() -> None:
    """httpx2 reads a followed redirect's body inside its own loop, below the cap; the hook drops it unread."""
    wire = gzip.compress(b"A" * (8 * 1024 * 1024))

    def handle(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/start":
            headers = {"Content-Encoding": "gzip", "Location": "/landing"}
            return httpx2.Response(302, stream=httpx2.ByteStream(wire), headers=headers)
        return httpx2.Response(200, content=b"fine")

    c = _Client(max_response_bytes=8192, transport=httpx2.MockTransport(handle))
    try:
        resp = await c.get("start", expect=RESPONSE)
    finally:
        await c.aclose()
    assert resp.content == b"fine"
    assert [len(h.content) for h in resp.history] == [0], "the 8 MiB redirect body was held in memory"


async def test_an_unfollowed_redirect_body_is_still_capped() -> None:
    """A redirect the client does not follow is the final response, so the ordinary cap applies to it."""
    wire = gzip.compress(b"A" * (8 * 1024 * 1024))

    def handle(request: httpx2.Request) -> httpx2.Response:
        headers = {"Content-Encoding": "gzip", "Location": "/landing"}
        return httpx2.Response(302, stream=httpx2.ByteStream(wire), headers=headers)

    c = _Client(max_response_bytes=8192, transport=httpx2.MockTransport(handle))
    try:
        with pytest.raises(ResponseTooLargeError):
            await c.get("start", expect=RESPONSE, follow_redirects=False)
    finally:
        await c.aclose()
