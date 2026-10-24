"""Tests for AsyncBaseClient failure policy — always raise a ClientError.

A true failure always raises the matching ``ClientError`` subtype. This is pure
transport: there is no ``None`` return — every call declares its ``expect=`` reader, and an
undeclared non-success status always raises rather than handing back a raw ``Response`` (there is
no raw mode). No-data interpretation is a CONTENT judgement for the caller; no status degrades to
``None`` anywhere in the client.
"""

from __future__ import annotations

from typing import Any

import httpx2
import pytest

from headgate._base import AsyncBaseClient
from headgate._rate_limiter import Rate
from headgate.errors import (
    AuthError,
    ClientTimeoutError,
    RateLimitError,
    ResponseParseError,
    TransportError,
    UpstreamStatusError,
)
from headgate.msgspec import decoder


def _client(handler: httpx2.MockTransport) -> AsyncBaseClient:
    return AsyncBaseClient(base_url="https://example.test", rate=Rate.per_second(2), max_retries=1, transport=handler)


async def test_raises_upstream_status_on_server_error() -> None:
    transport = httpx2.MockTransport(lambda req: httpx2.Response(500))
    async with _client(transport) as c:
        with pytest.raises(UpstreamStatusError) as ei:
            await c.get("/x", expect=decoder(dict[str, Any]))
        assert ei.value.status_code == 500


async def test_raises_transport_on_network_error() -> None:
    def boom(req: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("refused", request=req)

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
    """httpx2's exceptions often stringify to ``""``.

    The live spreads journal kept ``[PublicClient] GET …/portfolio/v2: `` and nothing after the
    colon, so the one fact the operator needed — WHAT failed — was gone.
    """

    def boom(req: httpx2.Request) -> httpx2.Response:
        raise raised("", request=req)

    async with _client(httpx2.MockTransport(boom)) as c:
        with pytest.raises(expected) as ei:
            await c.get("/x", expect=decoder(dict[str, Any]))
    assert raised.__name__ in str(ei.value)


async def test_raises_parse_error_on_bad_json() -> None:
    transport = httpx2.MockTransport(lambda req: httpx2.Response(200, content=b"not json"))
    async with _client(transport) as c:
        with pytest.raises(ResponseParseError):
            await c.get("/x", expect=decoder(dict[str, Any]))


# ---------- non-success: "retrieve or raise" (Stripe-style) ----------


@pytest.mark.parametrize("status", [404, 410])
async def test_missing_resource_raises(status: int) -> None:
    """A 404/410 on a read is a missing resource; it surfaces as a ClientError
    (Stripe-style retrieve), not a silent None."""
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

    That attribution is why a caller can tell which upstream failed from the error alone,
    and it is the one thing the per-provider copies of this ladder asserted that a base
    test would miss — so it is pinned here, once, on a subclass rather than on
    ``AsyncBaseClient`` itself. Every one of the 84 clients inherits the same mechanism.
    """

    class NamedClient(AsyncBaseClient):
        pass

    transport = httpx2.MockTransport(lambda req: httpx2.Response(500))
    async with NamedClient(
        rate=Rate.per_second(2), base_url="https://example.test", max_retries=1, transport=transport
    ) as c:
        with pytest.raises(UpstreamStatusError) as ei:
            await c.get("/x", expect=decoder(dict[str, Any]))
        assert ei.value.source == "NamedClient"


async def test_rate_limited_raises_rate_limit_error() -> None:
    """429 after retries is its own subtype, not a bare UpstreamStatusError.

    Callers branch on it — a rate limit is a back-off signal, where a 400 or a 500 is
    not — so collapsing it into the parent would be a silent behaviour change. It is a
    subclass, so a handler catching UpstreamStatusError still sees it.

    Added: this ladder rung and the timeout below were the only two NOT
    pinned centrally, which is why 18 provider-tier suites had each grown their own copy
    of "429 becomes RateLimitError" against their own provider's client. That mapping is
    AsyncBaseClient's, identical for all 84 clients, so it belongs here once.
    """
    transport = httpx2.MockTransport(lambda req: httpx2.Response(429))
    async with _client(transport) as c:
        with pytest.raises(RateLimitError) as ei:
            await c.get("/x", expect=decoder(dict[str, Any]))
        assert ei.value.status_code == 429
        assert isinstance(ei.value, UpstreamStatusError), "must stay catchable as the parent"


async def test_timeout_raises_client_timeout_error() -> None:
    """A timeout is a TransportError subtype, distinguishable from a connection failure.

    The distinction is load-bearing at the REST boundary, which maps ClientTimeoutError
    to 504 and the rest of the tree to 502.
    """

    def _timeout(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.TimeoutException("upstream timeout", request=request)

    async with _client(httpx2.MockTransport(_timeout)) as c:
        with pytest.raises(ClientTimeoutError) as ei:
            await c.get("/x", expect=decoder(dict[str, Any]))
        assert isinstance(ei.value, TransportError), "must stay catchable as the parent"


# ---------- "must exist" reads: raising client returns a value on success ----------


async def test_returns_value_on_success() -> None:
    """A successful read returns its value (the must-exist read path: no raise,
    real payload back)."""
    transport = httpx2.MockTransport(lambda req: httpx2.Response(200, json={"a": 1}))
    async with _client(transport) as c:
        result = await c.get("/x", expect=decoder(dict[str, Any]))
        assert result == {"a": 1}
