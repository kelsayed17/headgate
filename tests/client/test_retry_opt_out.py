# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""``retry=False``, the opt-out for non-idempotent writes, disables retries entirely.

The flag is on every verb and installs a zero-retry policy (one send, no re-send) for that one request. It is a
keyword argument rather than a transport extension so it shows in the verb's signature and at the call site. These
tests pin the behaviour end to end, so a verb that silently stopped threading the flag fails here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx2
import pytest

from headgate._client import AsyncBaseClient
from headgate._rate_limit import Rate
from headgate.errors import UpstreamStatusError
from tests.support.responses import RESPONSE

if TYPE_CHECKING:
    from pytest_httpx2 import HTTPXMock


class _Probe(AsyncBaseClient):
    """A client with the default retry policy."""

    def __init__(self) -> None:
        """Build with the defaults: three attempts per request."""
        super().__init__(base_url="https://example.invalid", rate=Rate.per_second(2))


async def test_retry_false_sends_exactly_one_request(httpx2_mock: HTTPXMock) -> None:
    """Check that a 500 with ``retry=False`` surfaces after one attempt, not three."""
    httpx2_mock.add_response(status_code=httpx2.codes.INTERNAL_SERVER_ERROR)

    async with _Probe() as client:
        with pytest.raises(UpstreamStatusError):
            await client.post("orders", expect=RESPONSE, retry=False)

    assert len(httpx2_mock.get_requests()) == 1


async def test_retry_false_works_on_a_read_verb(httpx2_mock: HTTPXMock) -> None:
    """Check that the flag works on a read verb too, not just the write ones."""
    httpx2_mock.add_response(status_code=httpx2.codes.INTERNAL_SERVER_ERROR)

    async with _Probe() as client:
        with pytest.raises(UpstreamStatusError):
            await client.get("quote", expect=RESPONSE, retry=False)

    assert len(httpx2_mock.get_requests()) == 1


async def test_the_default_retries(httpx2_mock: HTTPXMock) -> None:
    """Check, as a control, that the same POST without the flag is re-sent: the hazard the flag closes.

    Without this the one-request assertions above would pass just as happily against a client that had stopped
    retrying altogether.
    """
    httpx2_mock.add_response(status_code=httpx2.codes.INTERNAL_SERVER_ERROR, is_reusable=True)

    async with _Probe() as client:
        with pytest.raises(UpstreamStatusError):
            await client.post("orders", expect=RESPONSE)

    assert len(httpx2_mock.get_requests()) > 1


async def test_retry_true_is_explicitly_the_default(httpx2_mock: HTTPXMock) -> None:
    """Check that passing the flag's default explicitly behaves as omitting it."""
    httpx2_mock.add_response(status_code=httpx2.codes.INTERNAL_SERVER_ERROR, is_reusable=True)

    async with _Probe() as client:
        with pytest.raises(UpstreamStatusError):
            await client.post("orders", expect=RESPONSE, retry=True)

    assert len(httpx2_mock.get_requests()) > 1


async def test_retry_false_and_a_cache_ttl_are_both_stamped(httpx2_mock: HTTPXMock) -> None:
    """Check that the two per-call extensions are stamped side by side: ``retry=False`` does not replace a TTL."""
    httpx2_mock.add_response(status_code=httpx2.codes.OK)
    ttl = 5.0

    async with _Probe() as client:
        await client.post("orders", expect=RESPONSE, retry=False, cache_ttl=ttl)

    sent = httpx2_mock.get_requests()[0]
    assert sent.extensions["hishel_ttl"] == ttl
    assert sent.extensions["retry"].total == 0
