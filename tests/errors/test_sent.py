# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""``ClientError.sent``: whether any byte of a failed call can have reached the network.

``False`` only when headgate knows none did; ``True`` when some may have, a transport that reports nothing included;
``None`` on an error no client call raised. It covers the call's own request, its retries and its followed redirects,
and never a token mint's hop, which is a request of its own.
"""

from __future__ import annotations

import asyncio
import contextlib
import math
import socket
from typing import TYPE_CHECKING, override

import httpx2
import pytest

from headgate import (
    BreakerPolicy,
    CachedTokenAuth,
    ClientError,
    HttpOptions,
    Json,
    Minted,
    RetryPolicy,
    Timeouts,
    TransportPolicy,
    as_bytes,
    body_reader,
    nothing,
)
from headgate.breaker import BreakerRegistry
from headgate.errors import CircuitOpenError, ClientTimeoutError, ResponseParseError, TransportError
from headgate.retry import Backoff
from tests.support.clients import StubClient

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable

_BASE = "https://api.example.test"
#: Retries that never wait.
_NO_WAIT = Backoff(factor=0.0, jitter=lambda: 1.0)


def _client(
    handler: Callable[[httpx2.Request], httpx2.Response],
    *,
    retry: RetryPolicy | None = None,
    auth: httpx2.Auth | None = None,
    breaker: BreakerPolicy | None = None,
) -> StubClient:
    """Return a client over *handler* that sends each call once unless *retry* says otherwise."""
    return StubClient(
        base_url=_BASE,
        auth=auth,
        policy=TransportPolicy(retry=retry, breaker=breaker),
        http=HttpOptions(transport=httpx2.MockTransport(handler), follow_redirects=True),
    )


async def _raised(client: StubClient, *, method: str = "GET") -> ClientError:
    """Return what one call through *client* raises: a GET, or a POST of a JSON body."""
    call = (
        client.get("orders", expect=as_bytes)
        if method == "GET"
        else client.post("orders", expect=nothing, body=Json({"qty": 1}))
    )
    with pytest.raises(ClientError) as raised:
        await call
    return raised.value


def _raising(exc: type[httpx2.TransportError]) -> Callable[[httpx2.Request], httpx2.Response]:
    """Return a handler that raises *exc*, as a transport that reports no send progress does."""

    def handler(request: httpx2.Request) -> httpx2.Response:
        msg = "failed"
        raise exc(msg, request=request)

    return handler


def test_an_error_no_call_raised_does_not_know() -> None:
    """Leave ``sent`` as ``None`` on an error built by hand, unless it is given."""
    assert ClientError("m").sent is None
    assert ClientError("m", sent=False).sent is False


@pytest.mark.parametrize(("exc", "sent"), [(httpx2.ConnectError, False), (httpx2.ReadError, True)])
async def test_a_transport_that_reports_nothing_is_judged_by_its_exception(
    exc: type[httpx2.TransportError], *, sent: bool
) -> None:
    """Read a failure that proves nothing left as unsent, and any other one as maybe sent."""
    error = await _raised(_client(_raising(exc)))

    assert isinstance(error, TransportError)
    assert error.sent is sent


@contextlib.asynccontextmanager
async def _silent_server() -> AsyncGenerator[str]:
    """Serve on a loopback port, reading each request and never answering it; yield the base URL."""

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        with contextlib.suppress(ConnectionError):
            await reader.read()
        writer.close()

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    host, port = server.sockets[0].getsockname()[:2]
    try:
        yield f"http://{host}:{port}"
    finally:
        server.close()
        await server.wait_closed()


def _closed_port() -> int:
    """Return a loopback port nothing listens on."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


async def test_a_refused_connection_on_the_network_sent_nothing() -> None:
    """Record nothing sent for a connection the network refused."""
    async with StubClient(base_url=f"http://127.0.0.1:{_closed_port()}", policy=TransportPolicy(retry=None)) as client:
        error = await _raised(client, method="POST")

    assert isinstance(error, TransportError)
    assert error.sent is False


async def test_a_read_timeout_after_the_body_was_written_may_have_been_sent() -> None:
    """Record a POST the server read and never answered as maybe sent: it may have been processed."""
    async with _silent_server() as url:
        policy = TransportPolicy(retry=None, timeout=Timeouts(read=0.05))
        async with StubClient(base_url=url, policy=policy) as client:
            error = await _raised(client, method="POST")

    assert isinstance(error, ClientTimeoutError)
    assert error.sent is True


async def test_a_parse_error_after_a_retried_503_was_sent() -> None:
    """Record a call that was answered as sent, whatever ends it afterwards."""
    answers = iter([httpx2.Response(503), httpx2.Response(200, content=b"x")])

    def reject(_body: bytes) -> bytes:
        msg = "not an order"
        raise ValueError(msg)

    client = _client(lambda _request: next(answers), retry=RetryPolicy(backoff=_NO_WAIT))
    with pytest.raises(ResponseParseError) as raised:
        await client.get("orders", expect=body_reader(reject))

    assert raised.value.sent is True


async def test_a_post_an_open_breaker_refused_sent_nothing() -> None:
    """Record nothing sent for a call the breaker refused before it was sent."""
    breaker = BreakerPolicy(failure_threshold=1, recovery_timeout=3600.0, registry=BreakerRegistry())
    client = _client(lambda _request: httpx2.Response(503), breaker=breaker)
    await _raised(client)

    error = await _raised(client, method="POST")

    assert isinstance(error, CircuitOpenError)
    assert (error.sent, error.attempts) == (False, 0)


async def test_a_redirect_whose_second_hop_fails_to_connect_was_sent() -> None:
    """Record a call as sent once its first hop was answered, however a later hop fails."""

    def answer(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/orders":
            return httpx2.Response(307, headers={"Location": "/elsewhere"})
        msg = "refused"
        raise httpx2.ConnectError(msg, request=request)

    error = await _raised(_client(answer))

    assert isinstance(error, TransportError)
    assert error.sent is True


class _HopMint(CachedTokenAuth):
    """Mint through the owning client's stack, with a hop to the token endpoint."""

    @override
    async def mint_flow(self, request: httpx2.Request) -> AsyncGenerator[httpx2.Request | Minted[str], httpx2.Response]:
        response = yield httpx2.Request("POST", f"{_BASE}/token")
        yield Minted(response.text, math.inf)


async def test_a_mint_hop_that_failed_before_the_calls_request_leaves_it_unsent() -> None:
    """Leave ``sent`` ``False`` when only the token hop reached the network: the call's own request never left."""

    def answer(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/token":
            msg = "token host dropped the connection"
            raise httpx2.ReadError(msg, request=request)
        return httpx2.Response(200)

    error = await _raised(_client(answer, auth=_HopMint()))

    assert isinstance(error, TransportError)
    assert error.sent is False


async def test_a_mint_hop_that_failed_after_the_calls_request_leaves_it_sent() -> None:
    """Leave ``sent`` ``True`` when the call's own request was answered 401 before the re-mint's hop failed."""

    def answer(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/token":
            msg = "connection refused"
            raise httpx2.ConnectError(msg, request=request)
        return httpx2.Response(401)

    auth = _HopMint()
    auth.seed("revoked", math.inf)  # of unknown age, so a 401 on it mints again
    error = await _raised(_client(answer, auth=auth))

    assert isinstance(error, TransportError)
    assert error.sent is True
