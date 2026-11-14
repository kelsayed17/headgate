# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""What a ``stream()`` call, a middleware's own answer and a token re-mint charge their origin's breaker with.

* a stream settles the half-open probe at its response head, so a long-lived stream does not refuse every other call
  to its origin until its block exits; a failure mid-body is then one ordinary failure;
* a stream that opens with a 4xx is the origin answering, as it is for a verb;
* a network ``TimeoutError`` while reading a refused stream's error head is the network's, not the call's deadline;
* an error another call raises inside a stream's block is that call's, not the stream's;
* a response a middleware answers without sending tested nothing;
* the call's deadline passing while its auth re-mints a token after a 401 is not the API origin's failure.
"""

from __future__ import annotations

import asyncio
import math
from typing import TYPE_CHECKING, ClassVar, override

import httpx2
import pytest

from headgate import BreakerPolicy, CachedTokenAuth, HttpOptions, Minted, TransportPolicy
from headgate._core import BreakerState, CallFinished
from headgate.breaker import BreakerRegistry, CircuitBreaker
from headgate.errors import ClientError, ClientTimeoutError, TransportError, UpstreamStatusError
from headgate.ratelimit import Rate
from headgate.retry import NO_RETRY
from headgate.testing import TestClock
from tests.support.clients import StubClient
from tests.support.events import Recorder
from tests.support.responses import RESPONSE

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator

    from headgate import Middleware, Next

_ORIGIN = "https://streamed.example"
_RATE = Rate.per_second(1000)
_COOLDOWN = 60.0

#: The deadline of a call whose auth hangs while re-minting.
_DEADLINE = 0.05


class _Breakers:
    """A registry on a hand-driven clock, and the policy that addresses it."""

    def __init__(self, threshold: int) -> None:
        """Trip after *threshold* failures; admit a probe a minute later."""
        self.clock = TestClock()
        registry = BreakerRegistry(clock=self.clock)
        self.policy = BreakerPolicy(failure_threshold=threshold, recovery_timeout=_COOLDOWN, registry=registry)
        self.breaker: CircuitBreaker = registry.breaker_for(
            _ORIGIN, failure_threshold=threshold, recovery_timeout=_COOLDOWN
        )

    def client(
        self,
        transport: httpx2.AsyncBaseTransport,
        *,
        observers: tuple[Recorder, ...] = (),
        middleware: tuple[Middleware, ...] = (),
    ) -> StubClient:
        """Return a client over *transport* that sends each call once, on this registry."""
        return StubClient(
            base_url=_ORIGIN,
            rate=_RATE,
            policy=TransportPolicy(retry=NO_RETRY, breaker=self.policy),
            http=HttpOptions(transport=transport),
            observers=observers,
            middleware=middleware,
        )

    async def trip(self) -> None:
        """Open the breaker with 503s, then let its cooldown elapse."""
        failing = self.client(httpx2.MockTransport(lambda _: httpx2.Response(503)))
        while self.breaker.state is not BreakerState.OPEN:
            with pytest.raises(UpstreamStatusError):
                await failing.get("x", expect=RESPONSE)
        self.clock.advance(_COOLDOWN)


def _answering(status: int, stream: httpx2.AsyncByteStream | None = None) -> httpx2.MockTransport:
    """Return a transport answering every request with *status*, over *stream* when given."""
    return httpx2.MockTransport(lambda _: httpx2.Response(status, stream=stream or httpx2.ByteStream(b"{}")))


class _DiesMidBody(httpx2.AsyncByteStream):
    """A body that drops after its first chunk, or raises *error* at once."""

    def __init__(self, error: Exception | None = None) -> None:
        """Raise *error* before any byte, or a dropped connection after the first."""
        self._error = error

    @override
    async def __aiter__(self) -> AsyncIterator[bytes]:
        if self._error is not None:
            raise self._error
        yield b"{"
        msg = "connection dropped mid-body"
        raise httpx2.ReadError(msg)


async def _drain(client: StubClient) -> None:
    """Stream ``events`` and read its whole body."""
    async with client.stream("events") as stream:
        async for _ in stream.aiter_bytes():
            pass


# --- The probe ---


async def test_a_stream_that_took_the_probe_closes_the_breaker_at_its_head() -> None:
    """Close the breaker once the stream's head passes, so other calls to the origin go out while it is read."""
    breakers = _Breakers(threshold=1)
    await breakers.trip()
    client = breakers.client(_answering(200))
    async with client.stream("events"):
        assert breakers.breaker.state is BreakerState.CLOSED
        assert (await client.get("x", expect=RESPONSE)).status_code == httpx2.codes.OK


async def test_a_stream_failing_mid_body_after_its_probe_closed_counts_one_ordinary_failure() -> None:
    """Count a body that drops after the probe's head as one failure of the closed breaker, not a failed probe."""
    breakers = _Breakers(threshold=2)
    await breakers.trip()
    client = breakers.client(_answering(200, _DiesMidBody()))
    with pytest.raises(TransportError):
        await _drain(client)
    assert breakers.breaker.state is BreakerState.CLOSED
    assert breakers.breaker.failure_count == 1


# --- Routed like a verb ---


async def test_a_stream_opened_with_a_4xx_is_the_origin_answering() -> None:
    """Reset the failure count for a 404, as a verb's 404 does: the origin is up."""
    breakers = _Breakers(threshold=2)
    failing = breakers.client(_answering(503))
    with pytest.raises(UpstreamStatusError):
        await failing.get("x", expect=RESPONSE)
    with pytest.raises(ClientError):
        async with breakers.client(_answering(404)).stream("missing"):
            pytest.fail("a 404 does not open the stream")
    assert breakers.breaker.failure_count == 0


async def test_a_timeout_reading_an_error_head_is_the_networks_not_the_deadline() -> None:
    """Map a network ``TimeoutError`` in a refused stream's error head as the exchange does, not as the deadline."""
    breakers = _Breakers(threshold=2)
    client = StubClient(
        base_url=_ORIGIN,
        rate=_RATE,
        policy=TransportPolicy(retry=NO_RETRY, breaker=breakers.policy, deadline=60.0),
        http=HttpOptions(transport=_answering(404, _DiesMidBody(TimeoutError("read timed out")))),
    )
    with pytest.raises(ClientError) as raised:
        async with client.stream("missing"):
            pytest.fail("a 404 does not open the stream")
    assert "deadline exceeded" not in str(raised.value)
    assert breakers.breaker.failure_count == 1, "the read failed on the wire"


async def test_another_calls_error_inside_a_stream_block_is_not_the_streams() -> None:
    """Report a stream that another call failed inside as a clean exchange, and leave that call's error alone."""
    recorder = Recorder()
    breakers = _Breakers(threshold=2)
    streaming = breakers.client(_answering(200), observers=(recorder,))
    other = breakers.client(_answering(503))
    with pytest.raises(UpstreamStatusError) as raised:
        async with streaming.stream("events"):
            await other.get("x", expect=RESPONSE)
    finished = recorder.of(CallFinished)
    assert [(event.status_code, event.error) for event in finished] == [(200, None)]
    assert len(raised.value.__notes__) == 1, "only the failing call's own attempt is noted"


# --- Answers the origin never gave ---


class _LocalAnswer:
    """A middleware that answers every attempt itself with a 503, never sending it."""

    credential_headers: ClassVar = frozenset[str]()
    credential_params: ClassVar = frozenset[str]()

    async def __call__(self, request: httpx2.Request, call_next: Next) -> httpx2.Response:
        """Answer *request* locally."""
        del call_next
        return httpx2.Response(503, request=request)


async def test_a_middlewares_own_answer_is_not_charged_to_the_origin() -> None:
    """Leave the breaker closed however often a middleware answers 503 without sending."""
    breakers = _Breakers(threshold=1)
    client = breakers.client(_answering(200), middleware=(_LocalAnswer(),))
    for _ in range(3):
        with pytest.raises(UpstreamStatusError):
            await client.get("x", expect=RESPONSE)
    assert breakers.breaker.state is BreakerState.CLOSED
    assert breakers.breaker.failure_count == 0


class _HangingMint(CachedTokenAuth):
    """Holds a revoked token; re-minting it hangs, in its own code or on a hop through the client's stack."""

    def __init__(self, *, hop: bool) -> None:
        """Seed the revoked token; mint over a hop when *hop*."""
        super().__init__()
        self._hop = hop
        self.seed("revoked", math.inf)

    @override
    async def mint_token(self) -> tuple[str, float]:
        await asyncio.sleep(3600)
        return "never", math.inf

    @override
    async def mint_flow(self, request: httpx2.Request) -> AsyncGenerator[httpx2.Request | Minted[str], httpx2.Response]:
        if not self._hop:
            token, lifetime = await self.mint_token()
            yield Minted(token, lifetime)
            return
        yield httpx2.Request("POST", "https://token.example/mint")
        yield Minted("never", math.inf)


class _ApiThenHangingTokenHost(httpx2.AsyncBaseTransport):
    """Answer the API origin 401; never answer the token host."""

    @override
    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        if request.url.host == "token.example":
            await asyncio.sleep(3600)
        return httpx2.Response(401)


@pytest.mark.parametrize("hop", [False, True], ids=["mint_token", "mint_flow hop"])
async def test_a_deadline_passing_during_a_401_re_mint_is_not_charged_to_the_origin(*, hop: bool) -> None:
    """Charge the API origin nothing when the deadline cuts off a re-mint: the token host is what hung."""
    breakers = _Breakers(threshold=1)
    client = StubClient(
        base_url=_ORIGIN,
        rate=_RATE,
        auth=_HangingMint(hop=hop),
        policy=TransportPolicy(retry=NO_RETRY, breaker=breakers.policy, deadline=_DEADLINE),
        http=HttpOptions(transport=_ApiThenHangingTokenHost()),
    )
    with pytest.raises(ClientTimeoutError):
        await client.get("x", expect=RESPONSE)
    assert breakers.breaker.state is BreakerState.CLOSED
    assert breakers.breaker.failure_count == 0
