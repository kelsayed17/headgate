# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""What the per-host circuit breaker counts, and who it lets through, tested through ``AsyncBaseClient``.

The breaker stops calling a host that is failing and finds out cheaply when it has recovered. These properties depend
on the seam (what ``_sent`` marks, and that the tracked block spans the body read) as much as on the state machine, so
they are driven end-to-end rather than against the breaker object:

* a cancelled request is neither a success nor a failure: the caller gave up, the host said nothing. A
  ``CancelledError`` is what every ``asyncio.wait_for`` around a client call raises;
* when the cooldown elapses exactly one request is admitted to test the host, not every request that is waiting;
* a cancelled probe frees the slot again, or the breaker would refuse everything forever;
* a failure while reading the body counts, like any other transport failure. The body is read after the response
  headers arrive, so a breaker around the transport alone would have seen a success;
* a retried fetch is one verdict, and a failure the host never saw (an auth flow, the size cap, a cache hit) is none.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, override

import httpx2
import pytest

from headgate import HttpOptions, TransportPolicy
from headgate.breaker import DEFAULT_REGISTRY, BreakerState, CircuitBreaker
from headgate.errors import (
    CircuitOpenError,
    ClientConfigurationError,
    ClientError,
    ResponseTooLargeError,
    TransportError,
    UpstreamStatusError,
)
from headgate.policy import policy_disabled
from headgate.ratelimit import Rate
from headgate.retry import RetryPolicy
from tests.support.clients import StubClient
from tests.support.responses import RESPONSE

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator

_HOST = "api.example"
_URL = f"https://{_HOST}"

#: The fixture breaker's threshold, and one less: the most failures it holds while closed.
_THRESHOLD = 5
_BELOW_THRESHOLD = _THRESHOLD - 1

#: The fixture breaker's cooldown, and a sleep that outlasts it.
_COOLDOWN = 0.05
_PAST_COOLDOWN = 0.06

#: How long a caller waits on a hanging request before giving up.
_CALLER_TIMEOUT = 0.05

#: How many callers race for the probe slot once the cooldown elapses.
_RACERS = 3

#: Attempts a retrying client makes per fetch.
_ATTEMPTS = 3

#: More refusals than the threshold, to show none of them counts.
_MORE_THAN_THRESHOLD = _THRESHOLD + 1


class _ScriptedTransport(httpx2.AsyncBaseTransport):
    """Serves scripted statuses; ``hang=True`` never answers at all."""

    def __init__(self, *statuses: int, hang: bool = False) -> None:
        """Serve *statuses* in order (the last repeats), 200 when none are given."""
        self._statuses = list(statuses) or [200]
        self._hang = hang
        self.calls = 0

    @override
    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        self.calls += 1
        if self._hang:
            await asyncio.sleep(3600)
        status = self._statuses[min(self.calls - 1, len(self._statuses) - 1)]
        return httpx2.Response(status, content=b"{}")


class _TruncatedBodyStream(httpx2.AsyncByteStream):
    """A body that dies after the first chunk — the shape a dropped connection takes."""

    @override
    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield b"{"
        msg = "connection dropped mid-body"
        raise httpx2.ReadError(msg)


class _TruncatedBodyTransport(httpx2.AsyncBaseTransport):
    """Answer 200 with a body that dies mid-read."""

    @override
    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, stream=_TruncatedBodyStream(), headers={"content-type": "application/json"})


@pytest.fixture
def breaker() -> CircuitBreaker:
    """Build the test host's breaker first, fast-recovering (the default is 60s); the first builder's settings win.

    The autouse ``reset_circuit_breakers`` clears the registry around the test. The cooldown is 50 ms rather than the
    1 ms ``test_breaker.py`` uses, so that a test asserting a healthy client is not refused cannot pass on a breaker
    that opened and whose cooldown already elapsed, admitting the request as a probe.
    """
    return DEFAULT_REGISTRY.breaker_for(_HOST, failure_threshold=_THRESHOLD, recovery_timeout=_COOLDOWN)


async def _fail(client: StubClient, times: int) -> None:
    """Make *times* requests that each fail with a ``ClientError``."""
    for _ in range(times):
        with pytest.raises(ClientError):
            await client.get("x", expect=RESPONSE)


async def test_a_cancelled_request_neither_counts_nor_resets(breaker: CircuitBreaker) -> None:
    """Leave the record alone when the caller's own timeout cancels a request: it says nothing about the host."""
    client = StubClient(
        rate=Rate.per_second(2),
        base_url=_URL,
        policy=TransportPolicy(retry=RetryPolicy(attempts=1)),
        http=HttpOptions(transport=_ScriptedTransport(503)),
    )
    await _fail(client, _BELOW_THRESHOLD)
    assert breaker.failure_count == _BELOW_THRESHOLD

    hanging = StubClient(
        rate=Rate.per_second(2),
        base_url=_URL,
        policy=TransportPolicy(retry=RetryPolicy(attempts=1)),
        http=HttpOptions(transport=_ScriptedTransport(hang=True)),
    )
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(hanging.get("x", expect=RESPONSE), timeout=_CALLER_TIMEOUT)

    assert breaker.failure_count == _BELOW_THRESHOLD, "a cancelled call must not reset the failures already recorded"
    assert breaker.state is BreakerState.CLOSED

    await _fail(client, 1)
    assert breaker.state is BreakerState.OPEN, "the 5th real failure must still open the breaker"


async def test_only_one_request_probes_a_recovered_host(breaker: CircuitBreaker) -> None:
    """Admit a single probe once half-open, not every caller that happened to be waiting."""
    transport = _ScriptedTransport(503)
    client = StubClient(
        rate=Rate.per_second(2),
        base_url=_URL,
        policy=TransportPolicy(retry=RetryPolicy(attempts=1)),
        http=HttpOptions(transport=transport),
    )
    await _fail(client, _THRESHOLD)
    assert breaker.state is BreakerState.OPEN
    calls_before = transport.calls

    await asyncio.sleep(_PAST_COOLDOWN)
    results = await asyncio.gather(*(client.get("x", expect=RESPONSE) for _ in range(_RACERS)), return_exceptions=True)

    assert transport.calls == calls_before + 1, "exactly one probe reaches the host"
    assert all(isinstance(r, ClientError) for r in results), "the probe fails on a 503, the rest are refused"


async def test_a_cancelled_probe_frees_the_slot(breaker: CircuitBreaker) -> None:
    """Free the slot when the probe is cancelled, or the breaker waits forever on a probe that never reports."""
    failing = StubClient(
        rate=Rate.per_second(2),
        base_url=_URL,
        policy=TransportPolicy(retry=RetryPolicy(attempts=1)),
        http=HttpOptions(transport=_ScriptedTransport(503)),
    )
    await _fail(failing, _THRESHOLD)
    await asyncio.sleep(_PAST_COOLDOWN)

    hanging = StubClient(
        rate=Rate.per_second(2),
        base_url=_URL,
        policy=TransportPolicy(retry=RetryPolicy(attempts=1)),
        http=HttpOptions(transport=_ScriptedTransport(hang=True)),
    )
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(hanging.get("x", expect=RESPONSE), timeout=_CALLER_TIMEOUT)  # takes the probe slot

    healthy = _ScriptedTransport(200)
    recovered = StubClient(
        rate=Rate.per_second(2),
        base_url=_URL,
        policy=TransportPolicy(retry=RetryPolicy(attempts=1)),
        http=HttpOptions(transport=healthy),
    )
    resp = await recovered.get("x", expect=RESPONSE)

    assert resp.status_code == httpx2.codes.OK, "the next caller must be admitted as the probe"
    assert healthy.calls == 1
    assert breaker.state is BreakerState.CLOSED


async def test_a_body_read_failure_counts_as_a_failure(breaker: CircuitBreaker) -> None:
    """Count a connection dying mid-body: the breaker sees the whole fetch, not just the headers.

    The client streams the response and caps the body afterwards, so a connection that dies mid-body raises after any
    layer wrapped around the transport has already seen a 200.
    """
    client = StubClient(
        rate=Rate.per_second(2),
        base_url=_URL,
        policy=TransportPolicy(retry=RetryPolicy(attempts=1)),
        http=HttpOptions(transport=_TruncatedBodyTransport()),
    )

    with pytest.raises(TransportError):
        await client.get("x", expect=RESPONSE)

    assert breaker.failure_count == 1


async def test_a_retried_request_counts_one_failure(breaker: CircuitBreaker) -> None:
    """Record one verdict per fetch, however many times the retry layer re-sent it."""
    transport = _ScriptedTransport(503)
    client = StubClient(
        rate=Rate.per_second(2),
        base_url=_URL,
        policy=TransportPolicy(retry=RetryPolicy(attempts=_ATTEMPTS)),
        http=HttpOptions(transport=transport),
    )

    with pytest.raises(UpstreamStatusError):
        await client.get("x", expect=RESPONSE)

    assert transport.calls == _ATTEMPTS, "every attempt reached the wire"
    assert breaker.failure_count == 1, "but one failure against the host"


class _RefusingAuth(httpx2.Auth):
    """An auth flow that fails to mint, for a configuration reason the host never saw."""

    @override
    def async_auth_flow(self, request: httpx2.Request) -> AsyncGenerator[httpx2.Request, httpx2.Response]:
        """Refuse before producing a flow: httpx2 calls this, then iterates what it returns."""
        msg = "rotated credential refused"
        raise ClientConfigurationError(msg, source="test")


class _CachedHitTransport(httpx2.AsyncBaseTransport):
    """Serve what a cache hit looks like to the client: a 200 carrying hishel's from-cache marker."""

    def __init__(self) -> None:
        """Start with no calls served."""
        self.calls = 0

    @override
    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        self.calls += 1
        return httpx2.Response(200, content=b"{}", extensions={"hishel_from_cache": True})


async def test_an_auth_flow_failure_does_not_open_the_breaker_for_the_host(breaker: CircuitBreaker) -> None:
    """Leave the breaker alone on a refused token mint: counting it would open the breaker for every client."""
    refusing = StubClient(
        rate=Rate.per_second(2),
        base_url=_URL,
        auth=_RefusingAuth(),
        policy=TransportPolicy(retry=RetryPolicy(attempts=1)),
        http=HttpOptions(transport=_ScriptedTransport(200)),
    )
    for _ in range(_MORE_THAN_THRESHOLD):
        with pytest.raises(ClientConfigurationError):
            await refusing.get("x", expect=RESPONSE)

    assert breaker.failure_count == 0
    healthy = StubClient(
        rate=Rate.per_second(2),
        base_url=_URL,
        policy=TransportPolicy(retry=RetryPolicy(attempts=1)),
        http=HttpOptions(transport=_ScriptedTransport(200)),
    )
    assert (await healthy.get("x", expect=RESPONSE)).status_code == httpx2.codes.OK, (
        "a correctly configured client on the host is not refused"
    )


async def test_an_oversized_body_does_not_count_against_the_host(breaker: CircuitBreaker) -> None:
    """Leave the breaker alone on a body over the size cap: the cap is the client's own limit."""
    client = StubClient(
        rate=Rate.per_second(2),
        base_url=_URL,
        policy=TransportPolicy(retry=RetryPolicy(attempts=1)),
        http=HttpOptions(transport=_ScriptedTransport(200), max_response_bytes=1),
    )
    for _ in range(_MORE_THAN_THRESHOLD):
        with pytest.raises(ResponseTooLargeError):
            await client.get("x", expect=RESPONSE)

    assert breaker.failure_count == 0
    assert breaker.state is BreakerState.CLOSED


async def test_a_cache_hit_neither_resets_failures_nor_closes_a_probe(breaker: CircuitBreaker) -> None:
    """Treat a cache hit as untested: it neither wipes the failure count nor closes a probe the host never saw."""
    failing = StubClient(
        rate=Rate.per_second(2),
        base_url=_URL,
        policy=TransportPolicy(retry=RetryPolicy(attempts=1)),
        http=HttpOptions(transport=_ScriptedTransport(503)),
    )
    cached = StubClient(
        rate=Rate.per_second(2),
        base_url=_URL,
        policy=TransportPolicy(retry=RetryPolicy(attempts=1)),
        http=HttpOptions(transport=_CachedHitTransport()),
    )

    await _fail(failing, _BELOW_THRESHOLD)
    await cached.get("x", expect=RESPONSE)
    assert breaker.failure_count == _BELOW_THRESHOLD, "the hit must not wipe the failures already recorded"

    await _fail(failing, 1)
    assert breaker.state is BreakerState.OPEN
    await asyncio.sleep(_PAST_COOLDOWN)

    await cached.get("x", expect=RESPONSE)  # takes the probe slot, but tests nothing
    assert breaker.state is not BreakerState.CLOSED

    healthy = _ScriptedTransport(200)
    recovered = StubClient(
        rate=Rate.per_second(2),
        base_url=_URL,
        policy=TransportPolicy(retry=RetryPolicy(attempts=1)),
        http=HttpOptions(transport=healthy),
    )
    await recovered.get("x", expect=RESPONSE)
    assert healthy.calls == 1, "the next caller is admitted as the real probe"
    assert breaker.state is BreakerState.CLOSED


async def test_an_opted_out_client_is_not_refused_by_a_breaker_another_client_opened() -> None:
    """Skip the registry read, not only the recording, with ``breaker=None`` and with policy off.

    The registry is process-global and keyed by host, so a client that merely stopped recording
    would still be refused by a breaker some other client opened on the same host. Runs on the
    registry's own 60s breaker rather than the ``breaker`` fixture: a 50ms cooldown could elapse
    mid-test and admit the control below as a probe.
    """
    DEFAULT_REGISTRY.clear()
    try:
        await _fail(
            StubClient(
                rate=Rate.per_second(2),
                base_url=_URL,
                policy=TransportPolicy(retry=RetryPolicy(attempts=1)),
                http=HttpOptions(transport=_ScriptedTransport(503)),
            ),
            _THRESHOLD,
        )
        assert DEFAULT_REGISTRY[_HOST].state is BreakerState.OPEN

        refused = _ScriptedTransport(200)
        with pytest.raises(CircuitOpenError):
            await StubClient(
                rate=Rate.per_second(2),
                base_url=_URL,
                policy=TransportPolicy(retry=RetryPolicy(attempts=1)),
                http=HttpOptions(transport=refused),
            ).get("x", expect=RESPONSE)
        assert refused.calls == 0, "control: a client that consults the breaker is refused"

        no_breaker = _ScriptedTransport(200)
        await StubClient(
            rate=Rate.per_second(2),
            base_url=_URL,
            policy=TransportPolicy(retry=RetryPolicy(attempts=1), breaker=None),
            http=HttpOptions(transport=no_breaker),
        ).get("x", expect=RESPONSE)
        policy_off = _ScriptedTransport(200)
        with policy_disabled():
            policy_off_client = StubClient(
                rate=Rate.per_second(2),
                base_url=_URL,
                policy=TransportPolicy(retry=RetryPolicy(attempts=1)),
                http=HttpOptions(transport=policy_off),
            )
        await policy_off_client.get("x", expect=RESPONSE)

        assert no_breaker.calls == 1, "breaker=None must reach the network"
        assert policy_off.calls == 1, "a policy-off client must reach the network"
    finally:
        DEFAULT_REGISTRY.clear()
