"""A malformed URL raises as a ``ClientError``, not as httpx2's ``InvalidURL``.

httpx2's ``InvalidURL`` is not an ``HTTPError``, so it escaped ``AsyncBaseClient``'s mapping and reached the REST
boundary as a 500 rather than the 502 every other client failure maps to. It is also not a host failure, so it must
not reach the per-host circuit breaker.
"""

from __future__ import annotations

import pytest

from headgate._async_breaker import _host_breakers
from headgate._base import AsyncBaseClient
from headgate._rate_limiter import Rate
from headgate._responses import as_bytes
from headgate.errors import ClientError


class _Client(AsyncBaseClient):
    def __init__(self) -> None:
        super().__init__(base_url="https://example.test", rate=Rate.per_second(1000))


async def test_a_malformed_url_is_a_client_error_and_not_a_host_failure() -> None:
    _host_breakers.clear()
    client = _Client()
    try:
        with pytest.raises(ClientError, match="invalid URL"):
            await client.get("a\x00b", expect=as_bytes)
    finally:
        await client.aclose()
    assert _host_breakers["example.test"].failure_count == 0, "a malformed URL is not a host failure"
    _host_breakers.clear()
