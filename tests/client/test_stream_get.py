# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""``AsyncBaseClient.stream_get``: a body yielded as it arrives, through the same stack and failure ladder."""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx2
import pytest

from headgate import HttpOptions
from headgate._client import AsyncBaseClient
from headgate._rate_limit import Rate
from headgate._responses import RefusedError
from headgate.errors import ClientError, UpstreamStatusError

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from pytest_httpx2 import HTTPXMock


#: A cap far below the streamed body, and a body far past it.
_SMALL_CAP = 100
_BIG_BODY = 5000

#: An error message is a bounded snippet plus a short prefix, whatever the size of the error body.
_MESSAGE_BOUND = 1000


class _Client(AsyncBaseClient):
    """A client streaming from ``x.test``."""

    def __init__(self, *, max_response_bytes: int = 1024 * 1024) -> None:
        """Build with *max_response_bytes* as the buffered-body cap, which a stream ignores."""
        super().__init__(
            base_url="https://x.test",
            rate=Rate.per_second(100),
            http=HttpOptions(max_response_bytes=max_response_bytes),
        )


class _Refusing(AsyncBaseClient):
    """A client whose host check refuses every 200."""

    def __init__(self) -> None:
        """Build with a refusal check, which requires not following redirects."""
        super().__init__(
            base_url="https://x.test",
            rate=Rate.per_second(100),
            http=HttpOptions(
                refusal_check=lambda r: RefusedError("no") if r.status_code == httpx2.codes.OK else None,
                follow_redirects=False,
            ),
        )


async def _joined(chunks: AsyncIterator[bytes]) -> bytes:
    return b"".join([c async for c in chunks])


async def test_a_body_larger_than_the_response_cap_streams_through(httpx2_mock: HTTPXMock) -> None:
    """Check that a stream is not held to the buffered-body cap."""
    httpx2_mock.add_response(content=b"x" * _BIG_BODY)
    assert len(await _joined(_Client(max_response_bytes=_SMALL_CAP).stream_get("big.csv"))) == _BIG_BODY


async def test_a_large_error_body_is_read_only_to_a_snippet(httpx2_mock: HTTPXMock) -> None:
    """Check that a failed stream reads only a bounded head of its error body."""
    httpx2_mock.add_response(status_code=httpx2.codes.BAD_REQUEST, content=b"bad " * 50_000)
    with pytest.raises(UpstreamStatusError) as ei:
        await _joined(_Client().stream_get("big.csv"))
    assert ei.value.status_code == httpx2.codes.BAD_REQUEST
    assert len(str(ei.value)) < _MESSAGE_BOUND


async def test_a_refusal_check_runs_on_the_head_before_any_chunk(httpx2_mock: HTTPXMock) -> None:
    """Check that the host check refuses a stream before its first chunk is yielded."""
    httpx2_mock.add_response(content=b"data")
    stream = _Refusing().stream_get("f")
    with pytest.raises(ClientError):
        await anext(stream)


async def test_following_redirects_is_refused_for_a_client_with_a_refusal_check() -> None:
    """Check that a stream on a refusing client cannot turn redirect-following on."""
    with pytest.raises(ValueError, match="refusal"):
        await _joined(_Refusing().stream_get("f", follow_redirects=True))


async def test_a_5xx_that_outlasts_retries_raises(httpx2_mock: HTTPXMock) -> None:
    """Check that a 503 surviving every retry raises from the stream."""
    httpx2_mock.add_response(status_code=httpx2.codes.SERVICE_UNAVAILABLE, is_reusable=True)
    with pytest.raises(UpstreamStatusError) as ei:
        await _joined(_Client().stream_get("f"))
    assert ei.value.status_code == httpx2.codes.SERVICE_UNAVAILABLE


async def test_a_second_stream_works_after_a_failed_one(httpx2_mock: HTTPXMock) -> None:
    """Check that a failed stream releases its connection, so the next one runs."""
    httpx2_mock.add_response(status_code=httpx2.codes.NOT_FOUND)
    httpx2_mock.add_response(content=b"ok")
    c = _Client()
    with pytest.raises(UpstreamStatusError):
        await _joined(c.stream_get("a"))
    assert await _joined(c.stream_get("b")) == b"ok"
    assert [r.url for r in httpx2_mock.get_requests()] == [
        httpx2.URL("https://x.test/a"),
        httpx2.URL("https://x.test/b"),
    ]
