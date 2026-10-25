# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""A response's body, read up to a size cap, and the response rebuilt around it.

Every buffered call reads the whole body before a reader sees it, so an unbounded response is an OOM vector.
:data:`DEFAULT_MAX_RESPONSE_BYTES` is the ceiling and :func:`read_capped` is the one function that enforces it.

Every count here is of **decompressed** bytes: ``aiter_bytes()`` yields the body after content-decoding, and httpx2
decompresses in slices of at most 1 MiB, so a gzip bomb is counted before it has materialized
(``tests/client/test_response_size_cap.py`` pins that slice bound). Each reader closes the stream when it is done,
suppressing a close failure: closing a partly read compressed stream makes httpx2 finalize its decoder, which raises
``DecodingError`` on the truncated input, and that must not replace the error in flight.
"""

from __future__ import annotations

import contextlib

import httpx2

from headgate.errors import ResponseTooLargeError

__all__ = ["DEFAULT_MAX_RESPONSE_BYTES", "read_capped", "read_head"]

#: Ceiling on a single **decompressed** response body, in bytes. httpx2 has no body-size ceiling of its own
#: (``httpx2.Limits`` caps connections only), and a ~500 KB gzipped body can expand to 524 MB. Raise it per client
#: with ``max_response_bytes=``, or use ``stream_get`` for a body too large to hold.
DEFAULT_MAX_RESPONSE_BYTES = 32 * 1024 * 1024

#: Headers describing the on-the-wire encoding, dropped from a rebuilt response because its body is already decoded.
_WIRE_ENCODING_HEADERS = ("content-encoding", "content-length")


async def read_capped(streamed: httpx2.Response, cap: int, *, source: str, url: str) -> httpx2.Response:
    """Read *streamed*'s body up to *cap* decompressed bytes, close it, and return the response rebuilt around it.

    Raises :class:`~headgate.errors.ResponseTooLargeError` past the cap. ``Content-Length`` is checked first as a
    cheap refusal of an honestly oversized body, but it is never the control: it reports the compressed size (so a gzip
    bomb passes it) and is absent on a chunked response. The control is the running count of decoded bytes.

    Bodies httpx2 reads itself are outside this function: a followed redirect's body is dropped unread by
    :func:`~headgate._redirects.drop_followed_redirect_body`, and a 401 an auth flow answers by re-sending is read in
    full by httpx2 before the retry.
    """
    try:
        declared = streamed.headers.get("content-length")
        # isascii: str.isdigit() also accepts digits int() rejects, such as a latin-1 "²".
        if declared is not None and declared.isascii() and declared.isdigit() and int(declared) > cap:
            msg = f"declared response size {int(declared)} exceeds cap {cap} for {url}"
            raise ResponseTooLargeError(msg, source=source)
        content = await _read_past(streamed, cap)
    finally:
        await _close(streamed)
    if len(content) > cap:
        msg = f"response body exceeded cap {cap} bytes for {url}"
        raise ResponseTooLargeError(msg, source=source)
    return _rebuilt(streamed, content)


async def read_head(streamed: httpx2.Response, limit: int) -> httpx2.Response:
    """Read at most *limit* decompressed bytes of *streamed*, close it, and return the response rebuilt around them.

    For a streamed response that will not be consumed (a refusal or a non-success status), where only the status, the
    headers and a bounded snippet of the body feed the ``ClientError``. It never raises for a long body: the rest is
    simply not read.
    """
    try:
        content = await _read_past(streamed, limit)
    finally:
        await _close(streamed)
    return _rebuilt(streamed, content[:limit])


async def _read_past(streamed: httpx2.Response, limit: int) -> bytes:
    """Read *streamed* until more than *limit* bytes have arrived or the body ends, and return what was read.

    Stopping one chunk past *limit* lets the caller tell a body of exactly *limit* bytes from a longer one.
    """
    chunks: list[bytes] = []
    total = 0
    async for chunk in streamed.aiter_bytes():
        chunks.append(chunk)
        total += len(chunk)
        if total > limit:
            break
    return b"".join(chunks)


async def _close(streamed: httpx2.Response) -> None:
    """Close *streamed*, suppressing the ``DecodingError`` a truncated compressed stream raises on close."""
    with contextlib.suppress(httpx2.HTTPError):
        await streamed.aclose()


def _rebuilt(streamed: httpx2.Response, content: bytes) -> httpx2.Response:
    """Return a fully read copy of *streamed* carrying *content*, for readers that use ``.content`` or ``.json()``.

    Built on ``streamed.request``, not the request that was sent: httpx2 derives ``Response.url`` from the request,
    and after a followed redirect the two differ. A consumer that detects a bad key by the redirect it causes needs
    the URL that answered.
    """
    return httpx2.Response(
        status_code=streamed.status_code,
        headers=[(k, v) for k, v in streamed.headers.multi_items() if k.lower() not in _WIRE_ENCODING_HEADERS],
        content=content,
        request=streamed.request,
        history=streamed.history,
        extensions=streamed.extensions,
    )
