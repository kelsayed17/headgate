# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""``AsyncBaseClient``'s response-body size cap (``HttpOptions.max_response_bytes``).

The client buffers the whole response into memory before decoding, so an unbounded body is an OOM vector. The cap
has one non-obvious requirement: it must count **decompressed** bytes.

``Content-Length`` is the *compressed* size, so a header check alone is useless against a gzip bomb: ~500 KB on the
wire can expand to 524 MB. The header is also absent from chunked responses. And a compression-ratio heuristic would
false-positive on real data, since JSON legitimately compresses well over 10x. Hence: stream, count decoded bytes,
abort past the ceiling.
"""

from __future__ import annotations

import gzip
from typing import TYPE_CHECKING, override

import httpx2
import pytest

from headgate import HttpOptions
from headgate._body import DEFAULT_MAX_RESPONSE_BYTES
from headgate._client import AsyncBaseClient
from headgate._rate_limit import Rate
from headgate.errors import ClientError, ResponseTooLargeError
from tests.support.responses import RESPONSE

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from pytest_httpx2 import HTTPXMock


_KIB = 1024
_MIB = 1024 * _KIB

#: The cap most tests run under, and a body comfortably past it.
_CAP = 8 * _KIB
_OVER_CAP = 20_000

#: How much a gzip bomb fixture may weigh on the wire: the point is that it is tiny.
_TINY_WIRE = 100_000


class _Client(AsyncBaseClient):
    """A client whose only setting under test is its body cap."""

    def __init__(self, *, max_response_bytes: int, transport: httpx2.AsyncBaseTransport | None = None) -> None:
        """Build with *max_response_bytes*, over *transport* or the mocked network."""
        super().__init__(
            base_url="https://example.test",
            rate=Rate.per_second(1000),
            http=HttpOptions(max_response_bytes=max_response_bytes, transport=transport),
        )


class TestResponseSizeCap:
    """The cap on a buffered body."""

    async def test_body_under_cap_passes_through_intact(self, httpx2_mock: HTTPXMock) -> None:
        """Check that a body under the cap is returned whole."""
        body = b"x" * _KIB
        httpx2_mock.add_response(content=body)
        c = _Client(max_response_bytes=_CAP)
        try:
            resp = await c.get("thing", expect=RESPONSE)
            assert resp.content == body
            assert resp.status_code == httpx2.codes.OK
        finally:
            await c.aclose()

    async def test_body_over_cap_raises(self, httpx2_mock: HTTPXMock) -> None:
        """Check that a body past the cap raises ``ResponseTooLargeError``, a ``ClientError``."""
        httpx2_mock.add_response(content=b"x" * _OVER_CAP)
        c = _Client(max_response_bytes=_CAP)
        try:
            with pytest.raises(ResponseTooLargeError) as ei:
                await c.get("thing", expect=RESPONSE)
            assert isinstance(ei.value, ClientError), "caught by the same handler as every other client failure"
        finally:
            await c.aclose()

    async def test_gzip_bomb_is_capped_on_decompressed_size(self) -> None:
        """Check that a tiny wire body that explodes when decoded is capped on its decoded size.

        Driven through ``httpx2.MockTransport`` with a real ``ByteStream`` rather than ``httpx2_mock``:
        content-decoding only runs when the response has a *stream* (a response constructed with ``content=`` sets
        ``_content`` and ``aiter_bytes`` skips the decoder entirely), and this test is worthless unless the gzip is
        really decoded.
        """
        payload = b"A" * (8 * _MIB)
        wire = gzip.compress(payload)
        assert len(wire) < _TINY_WIRE, "fixture should be tiny on the wire"

        def handler(_request: httpx2.Request) -> httpx2.Response:
            return httpx2.Response(
                200,
                headers={
                    "Content-Encoding": "gzip",
                    "Content-Length": str(len(wire)),
                },
                stream=httpx2.ByteStream(wire),
            )

        c = _Client(max_response_bytes=_MIB, transport=httpx2.MockTransport(handler))
        try:
            with pytest.raises(ResponseTooLargeError):
                await c.get("bomb", expect=RESPONSE)
        finally:
            await c.aclose()

    async def test_httpx2_decodes_a_bomb_in_slices_of_at_most_one_mebibyte(self) -> None:
        """Check the httpx2 property ``read_capped`` relies on for peak memory, so a regression in httpx2 shows.

        The cap bounds the accumulated body; the transient peak is the largest single decoded chunk, which httpx2
        bounds at 1 MiB by decompressing with ``max_length`` (``httpx2._decoders.MAX_DECODE_CHUNK_SIZE``). Without
        that bound, one network read of a highly compressed bomb decodes into a single chunk of tens of megabytes.
        The wire is fed in 16 KiB reads, as a socket would deliver it: one ``ByteStream`` would hand the decoder the
        whole wire at once and say nothing about how a real stream is sliced.
        """
        wire = gzip.compress(b"\0" * (64 * _MIB), compresslevel=9)
        read_size = 16 * _KIB
        enough = 4 * _MIB

        class _Socket(httpx2.AsyncByteStream):
            """The wire, delivered in socket-sized reads."""

            @override
            async def __aiter__(self) -> AsyncIterator[bytes]:
                for i in range(0, len(wire), read_size):
                    yield wire[i : i + read_size]

        def handler(_request: httpx2.Request) -> httpx2.Response:
            return httpx2.Response(200, headers={"Content-Encoding": "gzip"}, stream=_Socket())

        biggest = total = 0
        async with (
            httpx2.AsyncClient(transport=httpx2.MockTransport(handler)) as http,
            http.stream("GET", "https://example.test/bomb") as resp,
        ):
            async for chunk in resp.aiter_bytes():
                biggest, total = max(biggest, len(chunk)), total + len(chunk)
                if total > enough:
                    break
        assert total > enough, "the bomb must actually decode for the bound to mean anything"
        assert biggest <= _MIB

    async def test_gzip_under_cap_decodes_intact(self) -> None:
        """Check that the cap neither corrupts nor truncates a legitimate compressed body."""
        payload = b"A" * (2 * _MIB)
        wire = gzip.compress(payload)

        def handler(_request: httpx2.Request) -> httpx2.Response:
            return httpx2.Response(
                200,
                headers={"Content-Encoding": "gzip"},
                stream=httpx2.ByteStream(wire),
            )

        c = _Client(max_response_bytes=16 * _MIB, transport=httpx2.MockTransport(handler))
        try:
            resp = await c.get("ok", expect=RESPONSE)
            assert resp.content == payload
            # The re-attached body is decoded, so the wire-encoding headers must not survive to misdescribe it.
            assert "content-encoding" not in resp.headers
        finally:
            await c.aclose()

    async def test_declared_content_length_rejects_before_transfer(self, httpx2_mock: HTTPXMock) -> None:
        """Check that an honestly oversized body is refused on its header, not downloaded."""
        httpx2_mock.add_response(content=b"x" * 100, headers={"Content-Length": str(_CAP * 1000)})
        c = _Client(max_response_bytes=_CAP)
        try:
            with pytest.raises(ResponseTooLargeError):
                await c.get("huge", expect=RESPONSE)
        finally:
            await c.aclose()

    async def test_missing_content_length_still_capped(self, httpx2_mock: HTTPXMock) -> None:
        """Check that a chunked response, which has no Content-Length, is capped on the decoded-byte count."""
        httpx2_mock.add_response(content=b"x" * _OVER_CAP, headers={"Transfer-Encoding": "chunked"})
        c = _Client(max_response_bytes=_CAP)
        try:
            with pytest.raises(ResponseTooLargeError):
                await c.get("chunked", expect=RESPONSE)
        finally:
            await c.aclose()

    def test_the_default_cap_is_at_least_8_mib(self) -> None:
        """Check that the default leaves headroom for multi-megabyte JSON payloads."""
        assert DEFAULT_MAX_RESPONSE_BYTES >= 8 * _MIB

    async def test_per_client_override_raises_the_ceiling(self, httpx2_mock: HTTPXMock) -> None:
        """Check that a client's own cap lets through a body past the usual test cap."""
        body = b"x" * _OVER_CAP
        httpx2_mock.add_response(content=body)
        c = _Client(max_response_bytes=_OVER_CAP)
        try:
            assert (await c.get("thing", expect=RESPONSE)).content == body
        finally:
            await c.aclose()


async def test_a_body_of_exactly_the_cap_passes(httpx2_mock: HTTPXMock) -> None:
    """Check that the cap is inclusive: a body of exactly ``max_response_bytes`` is returned whole."""
    body = b"x" * _CAP
    httpx2_mock.add_response(content=body)
    c = _Client(max_response_bytes=_CAP)
    try:
        assert (await c.get("thing", expect=RESPONSE)).content == body
    finally:
        await c.aclose()


async def test_a_content_length_int_cannot_parse_is_ignored_not_a_crash() -> None:
    """Check that a ``Content-Length`` of a digit ``int()`` rejects (latin-1 "²") falls through to the byte count.

    ``str.isdigit()`` accepts it, so a check on ``isdigit`` alone raised ``ValueError`` out of the client.
    """

    def handler(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(httpx2.codes.OK, headers=[(b"Content-Length", b"\xb2")], content=b"fine")

    c = _Client(max_response_bytes=_CAP, transport=httpx2.MockTransport(handler))
    try:
        assert (await c.get("thing", expect=RESPONSE)).content == b"fine"
    finally:
        await c.aclose()


async def test_a_followed_redirect_body_is_dropped_not_buffered() -> None:
    """Check that a followed redirect's body, which httpx2 reads below the cap, is dropped unread."""
    wire = gzip.compress(b"A" * (8 * _MIB))

    def handle(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/start":
            headers = {"Content-Encoding": "gzip", "Location": "/landing"}
            return httpx2.Response(httpx2.codes.FOUND, stream=httpx2.ByteStream(wire), headers=headers)
        return httpx2.Response(httpx2.codes.OK, content=b"fine")

    c = _Client(max_response_bytes=_CAP, transport=httpx2.MockTransport(handle))
    try:
        resp = await c.get("start", expect=RESPONSE)
    finally:
        await c.aclose()
    assert resp.content == b"fine"
    assert [len(h.content) for h in resp.history] == [0], "the 8 MiB redirect body was held in memory"


async def test_an_unfollowed_redirect_body_is_still_capped() -> None:
    """Check that a redirect the client does not follow is the final response, so the cap applies to it."""
    wire = gzip.compress(b"A" * (8 * _MIB))

    def handle(_request: httpx2.Request) -> httpx2.Response:
        headers = {"Content-Encoding": "gzip", "Location": "/landing"}
        return httpx2.Response(httpx2.codes.FOUND, stream=httpx2.ByteStream(wire), headers=headers)

    c = _Client(max_response_bytes=_CAP, transport=httpx2.MockTransport(handle))
    try:
        with pytest.raises(ResponseTooLargeError):
            await c.get("start", expect=RESPONSE, follow_redirects=False)
    finally:
        await c.aclose()
