# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""``AsyncBaseClient.stream_get`` — a body yielded as it arrives, through the same stack and failure ladder."""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx2
import pytest

from headgate._client import AsyncBaseClient
from headgate._rate_limit import Rate
from headgate._responses import Refused
from headgate.errors import ClientError, UpstreamStatusError

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from pytest_httpx2 import HTTPXMock


class _Client(AsyncBaseClient):
    def __init__(self, *, max_response_bytes: int = 1024 * 1024) -> None:
        super().__init__(base_url="https://x.test", rate=Rate.per_second(100), max_response_bytes=max_response_bytes)


async def _joined(chunks: AsyncIterator[bytes]) -> bytes:
    return b"".join([c async for c in chunks])


async def test_a_body_larger_than_the_response_cap_streams_through(httpx2_mock: HTTPXMock) -> None:
    httpx2_mock.add_response(content=b"x" * 5000)
    assert len(await _joined(_Client(max_response_bytes=100).stream_get("big.csv"))) == 5000


async def test_a_large_error_body_is_read_only_to_a_snippet(httpx2_mock: HTTPXMock) -> None:
    httpx2_mock.add_response(status_code=400, content=b"bad " * 50_000)
    with pytest.raises(UpstreamStatusError) as ei:
        await _joined(_Client().stream_get("big.csv"))
    assert ei.value.status_code == 400
    assert len(str(ei.value)) < 1000


async def test_a_refusal_check_runs_on_the_head_before_any_chunk(httpx2_mock: HTTPXMock) -> None:
    class Refusing(AsyncBaseClient):
        def __init__(self) -> None:
            super().__init__(
                base_url="https://x.test",
                rate=Rate.per_second(100),
                refusal_check=lambda r: Refused("no") if r.status_code == 200 else None,
                follow_redirects=False,
            )

    httpx2_mock.add_response(content=b"data")
    stream = Refusing().stream_get("f")
    with pytest.raises(ClientError):
        await anext(stream)


async def test_following_redirects_is_refused_for_a_client_with_a_refusal_check() -> None:
    class Refusing(AsyncBaseClient):
        def __init__(self) -> None:
            super().__init__(
                base_url="https://x.test",
                rate=Rate.per_second(100),
                refusal_check=lambda _r: None,
                follow_redirects=False,
            )

    with pytest.raises(ValueError, match="refusal"):
        await _joined(Refusing().stream_get("f", follow_redirects=True))


async def test_a_5xx_that_outlasts_retries_raises(httpx2_mock: HTTPXMock) -> None:
    httpx2_mock.add_response(status_code=503, is_reusable=True)
    with pytest.raises(UpstreamStatusError) as ei:
        await _joined(_Client().stream_get("f"))
    assert ei.value.status_code == 503


async def test_a_second_stream_works_after_a_failed_one(httpx2_mock: HTTPXMock) -> None:
    httpx2_mock.add_response(status_code=404)
    httpx2_mock.add_response(content=b"ok")
    c = _Client()
    with pytest.raises(UpstreamStatusError):
        await _joined(c.stream_get("a"))
    assert await _joined(c.stream_get("b")) == b"ok"
    assert [r.url for r in httpx2_mock.get_requests()] == [
        httpx2.URL("https://x.test/a"),
        httpx2.URL("https://x.test/b"),
    ]
