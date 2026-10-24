"""The non-idempotent-write escape hatch must disable retries entirely.

``retry=False`` on any verb installs a zero-retry policy (one send, no re-send) for that one request. It
replaced ``extensions=NO_RETRY`` (a ``MappingProxyType`` over
``headgate._no_retry``) in the clients audit: a flag is visible in the
verb's signature and greppable at the call site, where the transport extension was
neither. The module is deleted; the four unit tests that pinned the mapping's own shape
(zero attempts, single key, read-only, splattable) went with it, because the behaviour
they protected is now covered by the request-level tests below.

The STATIC half of this contract — which methods are required to pass the flag — lives
in a downstream architecture test. These pin the BEHAVIOUR, so
a signature that silently stopped threading the flag fails here rather than passing an
AST check.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from headgate._base import AsyncBaseClient
from headgate._rate_limiter import Rate
from headgate.errors import UpstreamStatusError
from tests.support.responses import RESPONSE

if TYPE_CHECKING:
    from pytest_httpx import HTTPXMock


class _Probe(AsyncBaseClient):
    def __init__(self) -> None:
        super().__init__(base_url="https://example.invalid", rate=Rate.per_second(2))


async def test_retry_false_sends_exactly_one_request(httpx_mock: HTTPXMock) -> None:
    """A 500 with ``retry=False`` must surface after ONE attempt, not three."""
    httpx_mock.add_response(status_code=500)

    async with _Probe() as client:
        with pytest.raises(UpstreamStatusError):
            await client.post("orders", expect=RESPONSE, retry=False)

    assert len(httpx_mock.get_requests()) == 1


async def test_retry_false_works_on_a_read_verb(httpx_mock: HTTPXMock) -> None:
    """The flag is on every verb, not just the write ones."""
    httpx_mock.add_response(status_code=500)

    async with _Probe() as client:
        with pytest.raises(UpstreamStatusError):
            await client.get("quote", expect=RESPONSE, retry=False)

    assert len(httpx_mock.get_requests()) == 1


async def test_the_default_retries(httpx_mock: HTTPXMock) -> None:
    """Control: the same POST without the flag IS re-sent — the vector the flag closes.

    Without this the one-request assertions above would pass just as happily against a
    client that had stopped retrying altogether.
    """
    httpx_mock.add_response(status_code=500, is_reusable=True)

    async with _Probe() as client:
        with pytest.raises(UpstreamStatusError):
            await client.post("orders", expect=RESPONSE)

    assert len(httpx_mock.get_requests()) > 1


async def test_retry_true_is_explicitly_the_default(httpx_mock: HTTPXMock) -> None:
    """Passing the flag's default explicitly must behave as omitting it."""
    httpx_mock.add_response(status_code=500, is_reusable=True)

    async with _Probe() as client:
        with pytest.raises(UpstreamStatusError):
            await client.post("orders", expect=RESPONSE, retry=True)

    assert len(httpx_mock.get_requests()) > 1


async def test_retry_false_and_a_cache_ttl_are_both_stamped(httpx_mock: HTTPXMock) -> None:
    """The two per-call extensions are stamped side by side — ``retry=False`` does not replace a TTL."""
    httpx_mock.add_response(status_code=200)

    async with _Probe() as client:
        await client.post("orders", expect=RESPONSE, retry=False, cache_ttl=5.0)

    sent = httpx_mock.get_requests()[0]
    assert sent.extensions["hishel_ttl"] == 5.0
    assert sent.extensions["retry"].total == 0
