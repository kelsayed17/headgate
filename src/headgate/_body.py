"""A response's body, read up to a size cap, and the response rebuilt around it.

Split out of ``_base.py`` earlier. Every client buffers the whole body before a reader sees it, so an unbounded
response is an OOM vector. :data:`DEFAULT_MAX_RESPONSE_BYTES` is the ceiling and :func:`read_capped` is the one
function that enforces it.
"""

from __future__ import annotations

import contextlib

import httpx2

from headgate.errors import ResponseTooLargeError

__all__ = ["DEFAULT_MAX_RESPONSE_BYTES", "read_capped", "read_head"]

#: Ceiling on a single **decompressed** response body, in bytes.
#:
#: Every client buffers the whole body before decoding, so an unbounded
#: response is an OOM vector: a ~500 KB gzipped body expands to 524 MB, and
#: ``httpx2.Limits`` caps connections only — httpx2 has no body-size ceiling.
#:
#: 32 MB is ~10x the largest payload measured live across every provider
#: (a ticker aggregate, 3.3 MB decompressed from 232 KB on the
#: wire). Raise it per client via ``max_response_bytes=`` for an endpoint
#: that legitimately streams more.
DEFAULT_MAX_RESPONSE_BYTES = 32 * 1024 * 1024

# Headers describing the ON-THE-WIRE encoding. They are dropped when the
# capped body is re-attached, because that body is already decoded — leaving
# ``Content-Encoding: gzip`` beside decompressed bytes (or a Content-Length
# measuring the compressed form) would misdescribe it.
_WIRE_ENCODING_HEADERS = ("content-encoding", "content-length")


async def read_capped(streamed: httpx2.Response, cap: int, *, source: str, url: str) -> httpx2.Response:
    """Read *streamed*'s body up to *cap* **decompressed** bytes, close it, and return the response rebuilt around it.

    ``Content-Length`` is checked first as a cheap refusal for an honestly oversized body — but it is only an
    optimization, never the control: it reports the *compressed* size (so a gzip bomb sails through it), and it is
    absent entirely on a chunked response like some filing indexes. The load-bearing check is the running total
    over ``aiter_bytes()``, which yields bytes *after* content-decoding.

    **What this does and does not bound.** The *accumulated* body is bounded by ``cap`` — that is the OOM fix, and
    httpx2 has no body-size ceiling of its own, so it stays this function's job. Peak *transient* memory is bounded by
    httpx2 itself: since 2.12.0 (the fix for CVE-2026-84382) its content decoders decompress with ``max_length`` in
    slices of at most 1 MiB (``httpx2._decoders.MAX_DECODE_CHUNK_SIZE`` for gzip, deflate and zstd; half that for
    brotli) and hand them over one at a time, so this counter sees a bomb before it has materialized. Measured on
    httpx2 2.13: a 1 MiB cap stopped a 64 MiB (1028:1) gzip bomb at 2 MiB accumulated, and a 268 MB bomb
    peaked at 3.4 MB traced. Under encode/httpx2 0.28.1, which still behaves this way, the same cap let a
    single 33 MB chunk through — one socket read times the compression ratio, which is what this paragraph used to warn
    about. ``test_httpx2_decodes_a_bomb_in_slices_of_at_most_one_mebibyte`` in ``tests/test_response_size_cap.py``
    pins the slice bound, so a decoder regression fails a test instead of this docstring going stale again; the
    dependency floor is what guarantees the bounded decoder in the images.

    **Bodies httpx2 reads itself are outside this function.** A followed redirect's body never reaches here; it is
    dropped unread by :func:`~headgate._redirects.drop_followed_redirect_body`. A 401 that an auth flow answers by
    re-sending is read in full by httpx2 before the retry, and is NOT dropped: the same 401, when the flow declines to
    retry, is the final response, and its body carries the provider's own reason. That read is uncapped, under the same
    threat as above.

    **The stream is always closed**, and a close failure is suppressed: closing a partially-consumed COMPRESSED stream
    makes httpx2 finalize its decoder, which raises ``DecodingError`` on the truncated input ("inconsistent stream
    state"). An abort here is deliberate, so that must not replace the ``ResponseTooLargeError`` in flight. Closing a
    fully-read response is a no-op.

    **The rebuilt response** carries the capped bytes, because every reader downstream reads ``.content`` / ``.text`` /
    ``.json()`` off a fully-read response. It drops the wire-encoding headers (the bytes are already decoded) and is
    built on ``streamed.request``, NOT the request that was sent: httpx2 derives ``Response.url`` from the request, and
    after a followed redirect the two differ. Rebuilding with the ORIGINAL made every capped response report the URL we
    ASKED for rather than the one that answered — which would silently disable any consumer that detects a bad key by
    a redirect to an HTML page under HTTP 200. A rebuild that drops the chain is not the response it claims to be.
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
    return httpx2.Response(
        status_code=streamed.status_code,
        headers=[(k, v) for k, v in streamed.headers.multi_items() if k.lower() not in _WIRE_ENCODING_HEADERS],
        content=b"".join(chunks),
        request=streamed.request,
        history=streamed.history,
        extensions=streamed.extensions,
    )


async def read_head(streamed: httpx2.Response, limit: int) -> httpx2.Response:
    """Close *streamed* after reading at most *limit* decompressed bytes, and return the response rebuilt around them.

    The streaming call's counterpart to :func:`read_capped`, for a response that will NOT be consumed: a refusal or a
    non-success status, where only the status, the headers and a bounded snippet of the body feed the ``ClientError``
    (``body_snippet`` reads ``.text``, which an unread stream refuses). It never raises for a long body: the rest is
    simply not read, and the close failure a partly-read compressed stream raises is suppressed, as in
    :func:`read_capped`.
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
    return httpx2.Response(
        status_code=streamed.status_code,
        headers=[(k, v) for k, v in streamed.headers.multi_items() if k.lower() not in _WIRE_ENCODING_HEADERS],
        content=b"".join(chunks)[:limit],
        request=streamed.request,
        history=streamed.history,
        extensions=streamed.extensions,
    )
