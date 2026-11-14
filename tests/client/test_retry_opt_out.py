# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""``retry=None`` disables retries entirely, even for a request the policy would safely re-send.

The option is on every verb and installs a zero-retry policy (one send, no re-send) for that one request. It is a
call option rather than a transport extension so it shows in the verb's signature and at the call site. These tests
pin the behavior end to end, so a verb that silently stopped threading the option fails here. The controls use a
POST carrying an idempotency key, which the default policy does re-send.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx2
import pytest

from headgate._client import Client
from headgate._core import CallContext
from headgate.errors import UpstreamStatusError
from headgate.ratelimit import Rate
from headgate.retry._policy import RETRY_POLICY, SEND_ONCE
from tests.cache._helpers import stamped_ttl
from tests.support.responses import RESPONSE

if TYPE_CHECKING:
    from pytest_httpx2 import HTTPXMock


def _probe() -> Client:
    """Build with the defaults: three attempts per request."""
    return Client(base_url="https://example.invalid", rate=Rate.per_second(2))


async def test_retry_none_sends_exactly_one_request(httpx2_mock: HTTPXMock) -> None:
    """Check that a 500 with ``retry=None`` surfaces after one attempt, not three."""
    httpx2_mock.add_response(status_code=httpx2.codes.INTERNAL_SERVER_ERROR)

    async with _probe() as client:
        with pytest.raises(UpstreamStatusError):
            await client.post("orders", expect=RESPONSE, retry=None)

    assert len(httpx2_mock.get_requests()) == 1


async def test_retry_none_works_on_a_read_verb(httpx2_mock: HTTPXMock) -> None:
    """Check that the flag works on a read verb too, not just the write ones."""
    httpx2_mock.add_response(status_code=httpx2.codes.INTERNAL_SERVER_ERROR)

    async with _probe() as client:
        with pytest.raises(UpstreamStatusError):
            await client.get("quote", expect=RESPONSE, retry=None)

    assert len(httpx2_mock.get_requests()) == 1


async def test_the_default_retries(httpx2_mock: HTTPXMock) -> None:
    """Check, as a control, that a keyed POST without the flag is re-sent.

    Without this the one-request assertions above would pass just as happily against a client that had stopped
    retrying altogether.
    """
    httpx2_mock.add_response(status_code=httpx2.codes.INTERNAL_SERVER_ERROR, is_reusable=True)

    async with _probe() as client:
        with pytest.raises(UpstreamStatusError):
            await client.post("orders", expect=RESPONSE, idempotency_key="order-1")

    assert len(httpx2_mock.get_requests()) > 1


async def test_retry_none_wins_over_an_idempotency_key(httpx2_mock: HTTPXMock) -> None:
    """Check that the flag stops even a keyed POST, which the policy would otherwise re-send."""
    httpx2_mock.add_response(status_code=httpx2.codes.INTERNAL_SERVER_ERROR)

    async with _probe() as client:
        with pytest.raises(UpstreamStatusError):
            await client.post("orders", expect=RESPONSE, idempotency_key="order-1", retry=None)

    assert len(httpx2_mock.get_requests()) == 1


async def test_retry_none_and_a_cache_ttl_are_both_stamped(httpx2_mock: HTTPXMock) -> None:
    """Check that the two per-call overrides are stamped side by side: ``retry=None`` does not replace a TTL."""
    httpx2_mock.add_response(status_code=httpx2.codes.OK)
    ttl = 5.0

    async with _probe() as client:
        await client.post("orders", expect=RESPONSE, retry=None, cache_ttl=ttl)

    sent = httpx2_mock.get_requests()[0]
    context = CallContext.find(sent)
    assert stamped_ttl(sent.extensions) == ttl
    assert context is not None
    assert context.get(RETRY_POLICY) is SEND_ONCE
