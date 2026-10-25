# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""A ``CachedTokenAuth`` whose mint fails reports the token endpoint's failure, never the API call's.

A mint usually runs on a client of its own. Without the mapping, its ``ConnectError`` became a ``TransportError``
naming the API URL and was charged to the API host's circuit breaker, a ``raise_for_status()`` became a transport
error rather than the status it was, and an unreadable token response escaped the ``ClientError`` tree.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, override

import httpx2
import pytest

from headgate import CachedTokenAuth, HttpOptions, Minted, TransportPolicy
from headgate._responses import as_bytes
from headgate.errors import (
    AuthError,
    ClientConfigurationError,
    ClientTimeoutError,
    DecodeError,
    ResponseParseError,
    TransportError,
    UpstreamStatusError,
)
from headgate.ratelimit import UNLIMITED
from headgate.retry import RetryPolicy
from tests.support.breakers import failures
from tests.support.clients import StubClient

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator, Callable

_API_HOST = "api.example.test"
_EXCHANGE_URL = "https://auth.example.test/token?client_secret=S3CR3T"
#: The token URL as every message names it.
_LOGGED_EXCHANGE_URL = "https://auth.example.test/token?client_secret=%2A%2A%2A"


class _MintsOver(CachedTokenAuth):
    """Mint on a client of its own over *handler*, reading ``access_token`` from a checked JSON response."""

    def __init__(self, handler: Callable[[httpx2.Request], httpx2.Response]) -> None:
        """Send the token exchange to *handler*."""
        super().__init__()
        self._handler = handler

    @override
    async def _mint_token(self) -> tuple[str, float]:
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(self._handler)) as client:
            response = await client.post(_EXCHANGE_URL)
        response.raise_for_status()
        return str(response.json()["access_token"]), 3600.0


class _Raises(CachedTokenAuth):
    """Mint by raising *exc*, for a failure no real exchange produces on demand."""

    def __init__(self, exc: Exception) -> None:
        """Raise *exc* from every mint."""
        super().__init__()
        self._exc = exc

    @override
    async def _mint_token(self) -> tuple[str, float]:
        raise self._exc


class _FlowReadsJson(CachedTokenAuth):
    """Mint through the owning client's stack, reading the token out of the hop's JSON body."""

    @override
    async def _mint_flow(
        self, request: httpx2.Request
    ) -> AsyncGenerator[httpx2.Request | Minted[str], httpx2.Response]:
        response = yield httpx2.Request("POST", f"https://{_API_HOST}/token")
        yield Minted(response.json()["access_token"], 3600.0)


def _api(auth: httpx2.Auth, handler: Callable[[httpx2.Request], httpx2.Response] | None = None) -> StubClient:
    """Return a client of the API host signing with *auth*, every API call answering 200 unless *handler* says."""
    return StubClient(
        base_url=f"https://{_API_HOST}",
        rate=UNLIMITED,
        auth=auth,
        policy=TransportPolicy(retry=RetryPolicy(attempts=1)),
        http=HttpOptions(transport=httpx2.MockTransport(handler or (lambda _r: httpx2.Response(httpx2.codes.OK)))),
    )


async def _call(auth: httpx2.Auth, handler: Callable[[httpx2.Request], httpx2.Response] | None = None) -> None:
    """Make one API call signed by *auth*, closing the client afterwards."""
    async with _api(auth, handler) as client:
        await client.get("series", expect=as_bytes)


def _refused(request: httpx2.Request) -> httpx2.Response:
    """Fail the exchange as an unreachable token host does."""
    msg = "connection refused"
    raise httpx2.ConnectError(msg, request=request)


def _slow(request: httpx2.Request) -> httpx2.Response:
    """Fail the exchange as a token host that never answers does."""
    msg = "timed out"
    raise httpx2.ReadTimeout(msg, request=request)


async def test_an_unreachable_token_host_names_the_token_endpoint_and_spares_the_api_host() -> None:
    """Name the token endpoint (its secret redacted), chain the cause, and leave the API host's breaker untouched."""
    with pytest.raises(TransportError) as raised:
        await _call(_MintsOver(_refused))

    assert type(raised.value) is TransportError
    assert str(raised.value) == f"[_MintsOver] POST {_LOGGED_EXCHANGE_URL} (token mint): connection refused"
    assert isinstance(raised.value.__cause__, httpx2.ConnectError)
    assert failures(_API_HOST) == 0


async def test_a_token_host_that_times_out_is_a_client_timeout() -> None:
    """Map a timed-out exchange to ``ClientTimeoutError``, as a timed-out API call is."""
    with pytest.raises(ClientTimeoutError):
        await _call(_MintsOver(_slow))


@pytest.mark.parametrize(
    ("status", "kind"),
    [
        (httpx2.codes.UNAUTHORIZED, AuthError),
        (httpx2.codes.FORBIDDEN, AuthError),
        (httpx2.codes.BAD_REQUEST, UpstreamStatusError),
        (httpx2.codes.SERVICE_UNAVAILABLE, UpstreamStatusError),
    ],
)
async def test_a_checked_token_response_is_the_status_it_was(status: int, kind: type[UpstreamStatusError]) -> None:
    """Map a mint's ``raise_for_status()`` through the status ladder, naming the endpoint and its reason."""
    auth = _MintsOver(lambda _r: httpx2.Response(status, json={"error": "invalid_client"}))

    with pytest.raises(kind) as raised:
        await _call(auth)

    assert type(raised.value) is kind
    assert raised.value.status_code == status
    assert f"token mint failed for POST {_LOGGED_EXCHANGE_URL}" in str(raised.value)
    assert '"invalid_client"' in str(raised.value)
    assert isinstance(raised.value.__cause__, httpx2.HTTPStatusError)


class _Unread(httpx2.AsyncByteStream):
    """A body nothing has read yet."""

    @override
    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield b"never read"


async def test_a_streamed_token_response_checked_before_it_was_read_has_no_snippet() -> None:
    """Build the status error without a body snippet when the mint checked a streamed response it never read."""
    request = httpx2.Request("POST", _EXCHANGE_URL)
    unread = httpx2.Response(httpx2.codes.BAD_REQUEST, stream=_Unread(), request=request)
    status_error = httpx2.HTTPStatusError("400", request=request, response=unread)

    with pytest.raises(UpstreamStatusError) as raised:
        await _call(_Raises(status_error))

    assert str(raised.value) == f"[_Raises] token mint failed for POST {_LOGGED_EXCHANGE_URL} (status=400)"


@pytest.mark.parametrize(
    "auth",
    [
        pytest.param(_MintsOver(lambda _r: httpx2.Response(httpx2.codes.OK, content=b"<html>")), id="not-json"),
        pytest.param(_MintsOver(lambda _r: httpx2.Response(httpx2.codes.OK, json={})), id="no-token-field"),
        pytest.param(_Raises(DecodeError("token response did not match")), id="a-codec-decode-error"),
    ],
)
async def test_an_unreadable_token_response_is_a_parse_error(auth: CachedTokenAuth) -> None:
    """Map a token response the mint could not read to ``ResponseParseError``, chained to what it raised."""
    with pytest.raises(ResponseParseError, match=r"^\[_\w+\] unreadable token response: ") as raised:
        await _call(auth)

    assert raised.value.__cause__ is not None


async def test_an_unreadable_token_response_from_a_mint_flow_is_a_parse_error() -> None:
    """Map a mint flow's ``response.json()`` failure on its hop the same way."""
    hop_body = b"not json"
    with pytest.raises(ResponseParseError):
        await _call(_FlowReadsJson(), lambda _r: httpx2.Response(httpx2.codes.OK, content=hop_body))


@pytest.mark.parametrize(
    "exc",
    [
        pytest.param(OSError("no route to host"), id="a-bare-oserror"),
        pytest.param(httpx2.ConnectError("refused"), id="an-httpx2-error-without-a-request"),
    ],
)
async def test_a_mint_failure_that_names_no_request_names_the_token_endpoint(exc: Exception) -> None:
    """Name the endpoint generically when the failure carries no request to name."""
    with pytest.raises(TransportError, match=r"^\[_Raises\] token endpoint \(token mint\): ") as raised:
        await _call(_Raises(exc))

    assert raised.value.__cause__ is exc


async def test_a_client_error_from_a_mint_is_raised_as_it_is() -> None:
    """Pass a ``ClientError`` the mint raised itself (a refused credential rotation) through unchanged."""
    refusal = ClientConfigurationError("rotating credentials", source="Provider")

    with pytest.raises(ClientConfigurationError) as raised:
        await _call(_Raises(refusal))

    assert raised.value is refusal


async def test_a_bug_in_a_mint_propagates_as_itself() -> None:
    """Leave an exception outside the mapping alone: it is a bug in the mint, not a failure to report."""
    with pytest.raises(NotImplementedError, match="half-written mint"):
        await _call(_Raises(NotImplementedError("half-written mint")))
