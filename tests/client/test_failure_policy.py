# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""``AsyncBaseClient``'s failure ladder: every failure raises the matching ``ClientError`` subtype.

Every call declares its ``expect=`` reader, and an undeclared non-success status always raises rather than returning
``None`` or a raw ``Response``. Deciding that a response means "no data" is a content judgment left to the caller.
"""

from __future__ import annotations

import httpx2
import pytest

from headgate import HttpOptions, TransportPolicy
from headgate._client import AsyncBaseClient
from headgate.errors import (
    AuthError,
    ClientTimeoutError,
    RateLimitError,
    ResponseParseError,
    TransportError,
    UpstreamStatusError,
)
from headgate.msgspec import decoder
from headgate.ratelimit import Rate
from headgate.retry import RetryPolicy

_POLICY = TransportPolicy(retry=RetryPolicy(attempts=1))


def _client(transport: httpx2.MockTransport) -> AsyncBaseClient:
    """Return a client over *transport* that sends each request once."""
    return AsyncBaseClient(
        base_url="https://example.test", rate=Rate.per_second(2), policy=_POLICY, http=HttpOptions(transport=transport)
    )


def _answering(status: int, content: bytes = b"") -> httpx2.MockTransport:
    """Return a transport answering every request with *status* and *content*."""
    return httpx2.MockTransport(lambda _request: httpx2.Response(status, content=content))


async def test_raises_upstream_status_on_server_error() -> None:
    """Check that a 5xx raises ``UpstreamStatusError`` with its status."""
    transport = _answering(500)
    async with _client(transport) as c:
        with pytest.raises(UpstreamStatusError) as ei:
            await c.get("/x", expect=decoder(dict[str, int]))
        assert ei.value.status_code == httpx2.codes.INTERNAL_SERVER_ERROR


async def test_raises_transport_on_network_error() -> None:
    """Check that a connection failure raises ``TransportError``."""

    def boom(request: httpx2.Request) -> httpx2.Response:
        msg = "refused"
        raise httpx2.ConnectError(msg, request=request)

    transport = httpx2.MockTransport(boom)
    async with _client(transport) as c:
        with pytest.raises(TransportError):
            await c.get("/x", expect=decoder(dict[str, int]))


@pytest.mark.parametrize(
    ("raised", "expected"),
    [(httpx2.ReadError, TransportError), (httpx2.ReadTimeout, ClientTimeoutError)],
)
async def test_a_network_error_with_an_empty_message_still_names_what_failed(
    raised: type[httpx2.TransportError], expected: type[TransportError]
) -> None:
    """Check that an exception that stringifies to ``""`` is named by its type in the message.

    Otherwise the message would end ``GET /x: `` with nothing after the colon, dropping what failed.
    """

    def boom(request: httpx2.Request) -> httpx2.Response:
        msg = ""
        raise raised(msg, request=request)

    async with _client(httpx2.MockTransport(boom)) as c:
        with pytest.raises(expected) as ei:
            await c.get("/x", expect=decoder(dict[str, int]))
    assert raised.__name__ in str(ei.value)


async def test_raises_parse_error_on_bad_json() -> None:
    """Check that an undecodable 2xx raises ``ResponseParseError``."""
    transport = _answering(httpx2.codes.OK, b"not json")
    async with _client(transport) as c:
        with pytest.raises(ResponseParseError):
            await c.get("/x", expect=decoder(dict[str, int]))


# --- non-success statuses raise ---


@pytest.mark.parametrize("status", [httpx2.codes.NOT_FOUND, httpx2.codes.GONE])
async def test_missing_resource_raises(status: int) -> None:
    """Check that a 404/410 raises rather than returning ``None``."""
    transport = _answering(status)
    async with _client(transport) as c:
        with pytest.raises(UpstreamStatusError) as ei:
            await c.get("/x", expect=decoder(dict[str, int]))
        assert ei.value.status_code == status


@pytest.mark.parametrize("status", [httpx2.codes.UNAUTHORIZED, httpx2.codes.FORBIDDEN])
async def test_auth_status_raises_auth_error(status: int) -> None:
    """Check that 401/403 raise ``AuthError``: they name this client's credential."""
    transport = _answering(status)
    async with _client(transport) as c:
        with pytest.raises(AuthError) as ei:
            await c.get("/x", expect=decoder(dict[str, int]))
        assert ei.value.status_code == status


async def test_bad_request_raises_upstream_status() -> None:
    """Check that a 400, a malformed request, surfaces as ``UpstreamStatusError``."""
    transport = _answering(400)
    async with _client(transport) as c:
        with pytest.raises(UpstreamStatusError) as ei:
            await c.get("/x", expect=decoder(dict[str, int]))
        assert ei.value.status_code == httpx2.codes.BAD_REQUEST


async def test_the_error_names_the_client_class_that_raised_it() -> None:
    """Check that ``source`` names the subclass that raised, not the base.

    That attribution is how a caller tells which upstream failed from the error alone, so it is pinned on a
    subclass rather than on ``AsyncBaseClient`` itself.
    """

    class NamedClient(AsyncBaseClient):
        """A subclass whose name the error must carry."""

    transport = _answering(500)
    async with NamedClient(
        rate=Rate.per_second(2), base_url="https://example.test", policy=_POLICY, http=HttpOptions(transport=transport)
    ) as c:
        with pytest.raises(UpstreamStatusError) as ei:
            await c.get("/x", expect=decoder(dict[str, int]))
        assert ei.value.source == "NamedClient"


async def test_rate_limited_raises_rate_limit_error() -> None:
    """Check that a 429 after retries raises ``RateLimitError``, a subtype of ``UpstreamStatusError``.

    Callers branch on it — a rate limit is a back-off signal, where a 400 or a 500 is not. It is a subclass, so a
    handler catching UpstreamStatusError still sees it.
    """
    transport = _answering(429)
    async with _client(transport) as c:
        with pytest.raises(RateLimitError) as ei:
            await c.get("/x", expect=decoder(dict[str, int]))
        assert ei.value.status_code == httpx2.codes.TOO_MANY_REQUESTS
        assert isinstance(ei.value, UpstreamStatusError), "must stay catchable as the parent"


async def test_timeout_raises_client_timeout_error() -> None:
    """Check that a timeout raises ``ClientTimeoutError``, a ``TransportError`` subtype.

    A caller can then treat the two differently (an HTTP gateway would answer 504 for one and 502 for the other)
    while still catching both as ``TransportError``.
    """

    def _timeout(request: httpx2.Request) -> httpx2.Response:
        msg = "upstream timeout"
        raise httpx2.TimeoutException(msg, request=request)

    async with _client(httpx2.MockTransport(_timeout)) as c:
        with pytest.raises(ClientTimeoutError) as ei:
            await c.get("/x", expect=decoder(dict[str, int]))
        assert isinstance(ei.value, TransportError), "must stay catchable as the parent"


# --- success ---


async def test_returns_value_on_success() -> None:
    """Check that a successful read returns the decoded payload."""
    transport = _answering(httpx2.codes.OK, b'{"a": 1}')
    async with _client(transport) as c:
        result = await c.get("/x", expect=decoder(dict[str, int]))
        assert result == {"a": 1}
