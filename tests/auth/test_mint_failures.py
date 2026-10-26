# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""A ``CachedTokenAuth`` whose mint fails reports the token endpoint's failure, never the API call's.

A mint usually runs on a client of its own. Without the mapping, its ``ConnectError`` became a ``TransportError``
naming the API URL and was charged to the API host's circuit breaker, a ``raise_for_status()`` became a transport
error rather than the status it was, and an unreadable token response escaped the ``ClientError`` tree.
"""

from __future__ import annotations

import gzip
import math
from typing import TYPE_CHECKING, override

import httpx2
import pytest

from headgate import MAX_MINT_RESPONSE_BYTES, CachedTokenAuth, HttpOptions, Minted, Timeouts, TransportPolicy
from headgate._responses import as_bytes, nothing
from headgate.errors import (
    AuthError,
    ClientConfigurationError,
    ClientTimeoutError,
    DecodeError,
    ResponseParseError,
    ResponseTooLargeError,
    TransportError,
    UpstreamStatusError,
)
from headgate.ratelimit import UNLIMITED
from headgate.retry import RetryPolicy
from headgate.testing import FakeTransport
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


async def test_an_unreadable_token_response_without_a_message_names_the_error_type() -> None:
    """Name the exception type when the mint's failure has no message of its own."""
    with pytest.raises(ResponseParseError) as raised:
        await _call(_Raises(ValueError()))

    assert str(raised.value) == "[_Raises] unreadable token response: ValueError"


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


class _ThreeHops(CachedTokenAuth):
    """A mint that takes three requests through the client's own stack."""

    hops = 3

    @override
    async def _mint_flow(
        self, request: httpx2.Request
    ) -> AsyncGenerator[httpx2.Request | Minted[str], httpx2.Response]:
        del request
        for hop in range(self.hops):
            yield httpx2.Request("GET", f"https://login.example.test/step/{hop}")
        yield Minted("t", math.inf)


def _hopping_client(max_redirects: int) -> tuple[StubClient, FakeTransport]:
    """Return a client minting through :class:`_ThreeHops` with *max_redirects*, and its network."""
    fake = FakeTransport().route("GET", "/v1/x", 200)
    for hop in range(_ThreeHops.hops):
        fake.route("GET", f"/step/{hop}", 200)
    auth = _ThreeHops()
    http = HttpOptions(transport=fake, max_redirects=max_redirects)
    return StubClient(base_url="https://api.example.test/v1", rate=UNLIMITED, auth=auth, http=http), fake


async def test_a_mints_hops_count_toward_max_redirects() -> None:
    """Refuse a call whose mint takes more hops than ``max_redirects``: httpx2 keeps one history for both."""
    client, fake = _hopping_client(_ThreeHops.hops - 1)

    with pytest.raises(TransportError, match="Exceeded maximum allowed redirects"):
        await client.get("x", expect=nothing)

    assert len(fake.calls_to("GET", "/v1/x")) == 0


async def test_a_mint_within_max_redirects_completes() -> None:
    """Send the call once a mint of as many hops as ``max_redirects`` allows has finished."""
    client, fake = _hopping_client(_ThreeHops.hops)

    await client.get("x", expect=nothing)

    assert len(fake.calls) == _ThreeHops.hops + 1


class _Streamed(httpx2.AsyncByteStream):
    """A body delivered as the network delivers it: unread until something iterates it."""

    def __init__(self, *chunks: bytes) -> None:
        """Deliver *chunks* in order."""
        self._chunks = chunks

    @override
    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self._chunks:
            yield chunk


class _DropsMidBody(httpx2.AsyncByteStream):
    """A body whose connection drops after its first chunk."""

    @override
    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield b"{"
        msg = "connection dropped"
        raise httpx2.ReadError(msg)


def _token_host(body: httpx2.AsyncByteStream) -> Callable[[httpx2.Request], httpx2.Response]:
    """Answer the hop to ``/token`` with *body*, streamed, and every API call with a 200 echoing its bearer."""

    def handle(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/token":
            return httpx2.Response(httpx2.codes.OK, stream=body, headers={"Content-Type": "application/json"})
        return httpx2.Response(httpx2.codes.OK, text=request.headers.get("Authorization", ""))

    return handle


async def test_a_mint_flow_is_handed_its_hops_body_read() -> None:
    """Hand the flow its hop's response already read, so ``response.json()`` works on a streamed body."""
    async with _api(_FlowReadsJson(), _token_host(_Streamed(b'{"access_', b'token": "t-1"}'))) as client:
        bearer = await client.get("series", expect=as_bytes)

    assert bearer == b"Bearer t-1"


async def test_an_oversized_token_response_is_refused_as_too_large() -> None:
    """Refuse a hop's body past ``MAX_MINT_RESPONSE_BYTES`` as ``ResponseTooLargeError`` naming the token mint."""
    chunk = b" " * (64 * 1024)
    chunks = [chunk] * (MAX_MINT_RESPONSE_BYTES // len(chunk) + 1)
    with pytest.raises(ResponseTooLargeError, match=r"\(token mint\)") as raised:
        await _call(_FlowReadsJson(), _token_host(_Streamed(*chunks)))

    error = raised.value
    assert (error.source, error.method, error.url) == (
        "_FlowReadsJson",
        "POST",
        f"https://{_API_HOST}/token (token mint)",
    )


async def test_a_token_response_is_capped_after_decompression() -> None:
    """Count a gzip hop's decompressed bytes, so a small compressed body that inflates past the cap is refused."""
    bomb = gzip.compress(b"\0" * (MAX_MINT_RESPONSE_BYTES + 1))

    def handle(request: httpx2.Request) -> httpx2.Response:
        """Answer the token hop with the compressed body."""
        headers = {"Content-Type": "application/json", "Content-Encoding": "gzip"}
        return httpx2.Response(httpx2.codes.OK, stream=_Streamed(bomb), headers=headers, request=request)

    assert len(bomb) < MAX_MINT_RESPONSE_BYTES
    with pytest.raises(ResponseTooLargeError, match=r"\(token mint\)"):
        await _call(_FlowReadsJson(), handle)


async def test_a_token_response_whose_connection_drops_is_a_transport_error() -> None:
    """Map a failure reading a hop's body through ``mint_error``, naming the token endpoint."""
    with pytest.raises(
        TransportError, match=r"^\[_FlowReadsJson\] POST https://api\.example\.test/token \(token mint\)"
    ):
        await _call(_FlowReadsJson(), _token_host(_DropsMidBody()))


@pytest.mark.parametrize(
    "exc",
    [
        pytest.param(httpx2.ResponseNotRead(), id="a-response-not-read"),
        pytest.param(httpx2.StreamConsumed(), id="a-stream-read-twice"),
        pytest.param(httpx2.CookieConflict("two cookies named session"), id="a-cookie-conflict"),
    ],
)
async def test_a_misread_token_response_is_a_parse_error(exc: Exception) -> None:
    """Map what httpx2 raises for a response handled out of order, or an ambiguous cookie, to ``ResponseParseError``."""
    with pytest.raises(ResponseParseError, match=r"^\[_Raises\] unreadable token response: ") as raised:
        await _call(_Raises(exc))

    assert raised.value.__cause__ is exc


#: The client's timeouts in the timeout tests, every phase longer than their deadline.
_CALL_TIMEOUTS = Timeouts(connect=11.0, read=22.0, write=13.0, pool=14.0)
#: The deadline in the timeout tests, shorter than every phase of :data:`_CALL_TIMEOUTS`.
_SHORT_DEADLINE = 2.5
#: The timeouts a hop sets for itself in the timeout tests.
_HOP_TIMEOUTS = {"connect": 1.0, "read": 1.0, "write": 1.0, "pool": 1.0}


class _OneHop(CachedTokenAuth):
    """A mint of one hop, built with *extensions*, whose response is the token itself."""

    def __init__(self, extensions: dict[str, object] | None = None) -> None:
        """Build the hop with *extensions*."""
        super().__init__()
        self._extensions = extensions or {}

    @override
    async def _mint_flow(
        self, request: httpx2.Request
    ) -> AsyncGenerator[httpx2.Request | Minted[str], httpx2.Response]:
        response = yield httpx2.Request("POST", f"https://{_API_HOST}/token", extensions=self._extensions)
        yield Minted(response.text, 3600.0)


async def _hop_timeouts(auth: CachedTokenAuth, *, deadline: float | None) -> dict[str, float]:
    """Make one call minting through *auth* under *deadline*, and return the timeouts its hop was sent with."""
    seen: dict[str, dict[str, float]] = {}

    def handle(request: httpx2.Request) -> httpx2.Response:
        seen[request.url.path] = request.extensions["timeout"]
        return httpx2.Response(httpx2.codes.OK, text="t")

    client = StubClient(
        base_url=f"https://{_API_HOST}",
        rate=UNLIMITED,
        auth=auth,
        policy=TransportPolicy(retry=RetryPolicy(attempts=1), deadline=None),
        http=HttpOptions(transport=httpx2.MockTransport(handle), timeout=_CALL_TIMEOUTS),
    )
    async with client:
        # Per call: a client whose own timeouts outlast its deadline is refused when it is built.
        await client.get("series", expect=as_bytes, deadline=deadline)
    return seen["/token"]


async def test_a_hop_takes_the_calls_timeouts() -> None:
    """Send a hop that sets no timeout with the call's, so a token host that never answers cannot hang the call."""
    timeouts = await _hop_timeouts(_OneHop(), deadline=None)

    assert timeouts == {"connect": 11.0, "read": 22.0, "write": 13.0, "pool": 14.0}


async def test_a_hops_timeouts_are_cut_to_the_calls_deadline() -> None:
    """Cut every phase of a hop's timeouts to the time the call's deadline leaves."""
    timeouts = await _hop_timeouts(_OneHop(), deadline=_SHORT_DEADLINE)

    assert set(timeouts) == {"connect", "read", "write", "pool"}
    assert all(0 < limit <= _SHORT_DEADLINE for limit in timeouts.values())


async def test_a_hop_that_sets_its_own_timeouts_keeps_them() -> None:
    """Keep the timeouts a hop was built with, rather than the call's."""
    timeouts = await _hop_timeouts(_OneHop({"timeout": _HOP_TIMEOUTS}), deadline=None)

    assert timeouts == _HOP_TIMEOUTS
