"""``retry=False``, the escape hatch for non-idempotent writes, disables retries entirely.

The flag is on every verb and installs a zero-retry policy (one send, no re-send) for that one request. It is a
keyword argument rather than a transport extension so it shows in the verb's signature and at the call site. These
tests pin the behaviour end to end, so a verb that silently stopped threading the flag fails here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from headgate._client import AsyncBaseClient
from headgate._rate_limit import Rate
from headgate.errors import UpstreamStatusError
from tests.support.responses import RESPONSE

if TYPE_CHECKING:
    from pytest_httpx2 import HTTPXMock


class _Probe(AsyncBaseClient):
    def __init__(self) -> None:
        super().__init__(base_url="https://example.invalid", rate=Rate.per_second(2))


async def test_retry_false_sends_exactly_one_request(httpx2_mock: HTTPXMock) -> None:
    """A 500 with ``retry=False`` must surface after ONE attempt, not three."""
    httpx2_mock.add_response(status_code=500)

    async with _Probe() as client:
        with pytest.raises(UpstreamStatusError):
            await client.post("orders", expect=RESPONSE, retry=False)

    assert len(httpx2_mock.get_requests()) == 1


async def test_retry_false_works_on_a_read_verb(httpx2_mock: HTTPXMock) -> None:
    """The flag is on every verb, not just the write ones."""
    httpx2_mock.add_response(status_code=500)

    async with _Probe() as client:
        with pytest.raises(UpstreamStatusError):
            await client.get("quote", expect=RESPONSE, retry=False)

    assert len(httpx2_mock.get_requests()) == 1


async def test_the_default_retries(httpx2_mock: HTTPXMock) -> None:
    """Control: the same POST without the flag IS re-sent — the vector the flag closes.

    Without this the one-request assertions above would pass just as happily against a
    client that had stopped retrying altogether.
    """
    httpx2_mock.add_response(status_code=500, is_reusable=True)

    async with _Probe() as client:
        with pytest.raises(UpstreamStatusError):
            await client.post("orders", expect=RESPONSE)

    assert len(httpx2_mock.get_requests()) > 1


async def test_retry_true_is_explicitly_the_default(httpx2_mock: HTTPXMock) -> None:
    """Passing the flag's default explicitly must behave as omitting it."""
    httpx2_mock.add_response(status_code=500, is_reusable=True)

    async with _Probe() as client:
        with pytest.raises(UpstreamStatusError):
            await client.post("orders", expect=RESPONSE, retry=True)

    assert len(httpx2_mock.get_requests()) > 1


async def test_retry_false_and_a_cache_ttl_are_both_stamped(httpx2_mock: HTTPXMock) -> None:
    """The two per-call extensions are stamped side by side — ``retry=False`` does not replace a TTL."""
    httpx2_mock.add_response(status_code=200)

    async with _Probe() as client:
        await client.post("orders", expect=RESPONSE, retry=False, cache_ttl=5.0)

    sent = httpx2_mock.get_requests()[0]
    assert sent.extensions["hishel_ttl"] == 5.0
    assert sent.extensions["retry"].total == 0
