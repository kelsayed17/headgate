# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""``AsyncBaseClient``'s failure ladder: every failure raises the matching ``ClientError`` subtype.

Every call declares its ``expect=`` reader, and an undeclared non-success status always raises rather than returning
``None`` or a raw ``Response``. Deciding that a response means "no data" is a content judgement left to the caller.
"""

from __future__ import annotations

from typing import Any

import httpx2
import pytest

from headgate._client import AsyncBaseClient
from headgate._rate_limit import Rate
from headgate.errors import (
    AuthError,
    ClientTimeoutError,
    RateLimitError,
    ResponseParseError,
    TransportError,
    UpstreamStatusError,
)
from headgate.msgspec import decoder


def _client(transport: httpx2.MockTransport) -> AsyncBaseClient:
    return AsyncBaseClient(
        base_url="https://example.test", rate=Rate.per_second(2), max_attempts=1, transport=transport
    )


async def test_raises_upstream_status_on_server_error() -> None:
    transport = httpx2.MockTransport(lambda req: httpx2.Response(500))
    async with _client(transport) as c:
        with pytest.raises(UpstreamStatusError) as ei:
            await c.get("/x", expect=decoder(dict[str, Any]))
        assert ei.value.status_code == 500


async def test_raises_transport_on_network_error() -> None:
    def boom(req: httpx2.Request) -> httpx2.Response:
        msg = "refused"
        raise httpx2.ConnectError(msg, request=req)

    transport = httpx2.MockTransport(boom)
    async with _client(transport) as c:
        with pytest.raises(TransportError):
            await c.get("/x", expect=decoder(dict[str, Any]))


@pytest.mark.parametrize(
    ("raised", "expected"),
    [(httpx2.ReadError, TransportError), (httpx2.ReadTimeout, ClientTimeoutError)],
)
async def test_a_network_error_with_an_empty_message_still_names_what_failed(
    raised: type[httpx2.TransportError], expected: type[TransportError]
) -> None:
    """httpx2's exceptions often stringify to ``""``, so the message names the exception type instead.

    Otherwise the message would end ``GET /x: `` with nothing after the colon, dropping what failed.
    """

    def boom(req: httpx2.Request) -> httpx2.Response:
        msg = ""
        raise raised(msg, request=req)

    async with _client(httpx2.MockTransport(boom)) as c:
        with pytest.raises(expected) as ei:
            await c.get("/x", expect=decoder(dict[str, Any]))
    assert raised.__name__ in str(ei.value)


async def test_raises_parse_error_on_bad_json() -> None:
    transport = httpx2.MockTransport(lambda req: httpx2.Response(200, content=b"not json"))
    async with _client(transport) as c:
        with pytest.raises(ResponseParseError):
            await c.get("/x", expect=decoder(dict[str, Any]))


# --- non-success statuses raise ---


@pytest.mark.parametrize("status", [404, 410])
async def test_missing_resource_raises(status: int) -> None:
    """A 404/410 on a read is a missing resource; it raises rather than returning ``None``."""
    transport = httpx2.MockTransport(lambda req: httpx2.Response(status))
    async with _client(transport) as c:
        with pytest.raises(UpstreamStatusError) as ei:
            await c.get("/x", expect=decoder(dict[str, Any]))
        assert ei.value.status_code == status


@pytest.mark.parametrize("status", [401, 403])
async def test_auth_status_raises_auth_error(status: int) -> None:
    """401/403 are auth failures (our creds) — a more specific AuthError."""
    transport = httpx2.MockTransport(lambda req: httpx2.Response(status))
    async with _client(transport) as c:
        with pytest.raises(AuthError) as ei:
            await c.get("/x", expect=decoder(dict[str, Any]))
        assert ei.value.status_code == status


async def test_bad_request_raises_upstream_status() -> None:
    """A 400 is a malformed request (a bug): it surfaces."""
    transport = httpx2.MockTransport(lambda req: httpx2.Response(400))
    async with _client(transport) as c:
        with pytest.raises(UpstreamStatusError) as ei:
            await c.get("/x", expect=decoder(dict[str, Any]))
        assert ei.value.status_code == 400


async def test_the_error_names_the_client_class_that_raised_it() -> None:
    """``source`` is ``type(self).__name__``, so it names the SUBCLASS, not the base.

    That attribution is how a caller tells which upstream failed from the error alone, so it is pinned on a
    subclass rather than on ``AsyncBaseClient`` itself.
    """

    class NamedClient(AsyncBaseClient):
        pass

    transport = httpx2.MockTransport(lambda req: httpx2.Response(500))
    async with NamedClient(
        rate=Rate.per_second(2), base_url="https://example.test", max_attempts=1, transport=transport
    ) as c:
        with pytest.raises(UpstreamStatusError) as ei:
            await c.get("/x", expect=decoder(dict[str, Any]))
        assert ei.value.source == "NamedClient"


async def test_rate_limited_raises_rate_limit_error() -> None:
    """429 after retries is its own subtype, not a bare UpstreamStatusError.

    Callers branch on it — a rate limit is a back-off signal, where a 400 or a 500 is not. It is a subclass, so a
    handler catching UpstreamStatusError still sees it.
    """
    transport = httpx2.MockTransport(lambda req: httpx2.Response(429))
    async with _client(transport) as c:
        with pytest.raises(RateLimitError) as ei:
            await c.get("/x", expect=decoder(dict[str, Any]))
        assert ei.value.status_code == 429
        assert isinstance(ei.value, UpstreamStatusError), "must stay catchable as the parent"


async def test_timeout_raises_client_timeout_error() -> None:
    """A timeout is a TransportError subtype, distinguishable from a connection failure.

    A caller can then treat the two differently (an HTTP gateway would answer 504 for one and 502 for the other)
    while still catching both as ``TransportError``.
    """

    def _timeout(request: httpx2.Request) -> httpx2.Response:
        msg = "upstream timeout"
        raise httpx2.TimeoutException(msg, request=request)

    async with _client(httpx2.MockTransport(_timeout)) as c:
        with pytest.raises(ClientTimeoutError) as ei:
            await c.get("/x", expect=decoder(dict[str, Any]))
        assert isinstance(ei.value, TransportError), "must stay catchable as the parent"


# --- success ---


async def test_returns_value_on_success() -> None:
    """A successful read returns the decoded payload."""
    transport = httpx2.MockTransport(lambda req: httpx2.Response(200, json={"a": 1}))
    async with _client(transport) as c:
        result = await c.get("/x", expect=decoder(dict[str, Any]))
        assert result == {"a": 1}
