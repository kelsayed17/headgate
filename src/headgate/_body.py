"""A response's body, read up to a size cap, and the response rebuilt around it.

Every buffered call reads the whole body before a reader sees it, so an unbounded response is an OOM vector.
:data:`DEFAULT_MAX_RESPONSE_BYTES` is the ceiling and :func:`read_capped` is the one function that enforces it.
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

#: Headers describing the ON-THE-WIRE encoding, dropped from a rebuilt response because its body is already decoded.
_WIRE_ENCODING_HEADERS = ("content-encoding", "content-length")


async def read_capped(streamed: httpx2.Response, cap: int, *, source: str, url: str) -> httpx2.Response:
    """Read *streamed*'s body up to *cap* **decompressed** bytes, close it, and return the response rebuilt around it.

    ``Content-Length`` is checked first as a cheap refusal for an honestly oversized body, but it is never the
    control: it reports the *compressed* size (so a gzip bomb sails through it) and is absent on a chunked response.
    The load-bearing check is the running total over ``aiter_bytes()``, which yields bytes after content-decoding.
    httpx2 (2.12.0 and later) decompresses in slices of at most 1 MiB, so the counter sees a bomb before it has
    materialized; ``tests/test_response_size_cap.py`` pins that slice bound.

    Bodies httpx2 reads itself are outside this function: a followed redirect's body is dropped unread by
    :func:`~headgate._redirects.drop_followed_redirect_body`, and a 401 an auth flow answers by re-sending is read in
    full by httpx2 before the retry.

    The stream is always closed, and a close failure is suppressed: closing a partly read COMPRESSED stream makes
    httpx2 finalize its decoder, which raises ``DecodingError`` on the truncated input and must not replace the
    ``ResponseTooLargeError`` in flight.
    """
    try:
        declared = streamed.headers.get("content-length")
        if declared is not None and declared.isdigit() and int(declared) > cap:
            raise ResponseTooLargeError(
                f"declared response size {int(declared)} exceeds cap {cap} for {url}", source=source
            )
        chunks: list[bytes] = []
        total = 0
        async for chunk in streamed.aiter_bytes():
            total += len(chunk)
            if total > cap:
                raise ResponseTooLargeError(f"response body exceeded cap {cap} bytes for {url}", source=source)
            chunks.append(chunk)
    finally:
        with contextlib.suppress(httpx2.HTTPError):
            await streamed.aclose()
    return _rebuilt(streamed, b"".join(chunks))


async def read_head(streamed: httpx2.Response, limit: int) -> httpx2.Response:
    """Close *streamed* after reading at most *limit* decompressed bytes, and return the response rebuilt around them.

    For a streamed response that will NOT be consumed (a refusal or a non-success status), where only the status, the
    headers and a bounded snippet of the body feed the ``ClientError``. It never raises for a long body: the rest is
    simply not read.
    """
    chunks: list[bytes] = []
    total = 0
    try:
        async for chunk in streamed.aiter_bytes():
            chunks.append(chunk)
            total += len(chunk)
            if total >= limit:
                break
    finally:
        with contextlib.suppress(httpx2.HTTPError):
            await streamed.aclose()
    return _rebuilt(streamed, b"".join(chunks)[:limit])


def _rebuilt(streamed: httpx2.Response, content: bytes) -> httpx2.Response:
    """A fully read copy of *streamed* carrying *content*, so readers can use ``.content`` / ``.text`` / ``.json()``.

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
