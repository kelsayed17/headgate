"""What the per-host circuit breaker counts, and who it lets through.

The breaker's job is to stop calling a host that is failing, and to find out cheaply when
it has recovered. Four properties carry that, and each is tested here end-to-end through
``AsyncBaseClient`` rather than against the breaker object, because three of the four were
wrong at the seam rather than in the state machine:

* a CANCELLED request is neither a success nor a failure — the caller gave up, the host
  said nothing;
* when the cooldown elapses exactly ONE request is admitted to test the host, not every
  request that happens to be waiting;
* a cancelled probe frees the slot again, or the breaker would reject everything forever;
* a failure while reading the BODY counts, like any other transport failure.

Previously the breaker was the ``circuitbreaker`` library driven by a ``with``
block inside a transport, and none of the four held: its ``__exit__`` treats anything that
is not an ``Exception`` as a success and calls ``reset()``, so a ``CancelledError`` — what
every ``asyncio.wait_for`` around a client call raises — wiped the failure count; its
half-open state admits every waiting caller; and the ``with`` block sat around the
response HEADERS, since the body is read afterwards by ``read_capped``.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, override

import httpx2
import pytest

from headgate._breaker import HOST_BREAKERS, BreakerState, CircuitBreaker
from headgate._client import AsyncBaseClient
from headgate._rate_limit import Rate
from headgate.policy import policy_disabled
from tests.support.responses import RESPONSE

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator, Iterator

_HOST = "api.example"
_URL = f"https://{_HOST}"


class _StubClient(AsyncBaseClient):
    """Minimal AsyncBaseClient subclass for breaker tests."""


class _ScriptedTransport(httpx2.AsyncBaseTransport):
    """Serves scripted statuses; ``hang=True`` never answers at all."""

    def __init__(self, *statuses: int, hang: bool = False) -> None:
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
        raise httpx2.ReadError("connection dropped mid-body")


class _TruncatedBodyTransport(httpx2.AsyncBaseTransport):
    @override
    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, stream=_TruncatedBodyStream(), headers={"content-type": "application/json"})


@pytest.fixture
def breaker() -> Iterator[CircuitBreaker]:
    """Register a fast-recovering breaker for the test host (the registry default is 60s).

    The tree-wide ``reset_circuit_breakers`` clears the registry around the test. The cooldown stays 50 ms rather
    than the 1 ms ``test_breaker.py`` uses: ``test_an_auth_flow_failure_does_not_open_the_breaker_for_the_host``
    asserts ``refusing`` inside it, and a shorter one would let that assertion pass on an opened breaker.
    """
    installed = CircuitBreaker(failure_threshold=5, recovery_timeout=0.05, name=_HOST)
    HOST_BREAKERS[_HOST] = installed
    yield installed


async def _fail(client: AsyncBaseClient, times: int) -> None:
    from headgate.errors import ClientError

    for _ in range(times):
        with pytest.raises(ClientError):
            await client.get("x", expect=RESPONSE)


async def test_a_cancelled_request_neither_counts_nor_resets(breaker: CircuitBreaker) -> None:
    """A caller's own timeout says nothing about the host, so it must not clear the record."""
    client = _StubClient(rate=Rate.per_second(2), base_url=_URL, transport=_ScriptedTransport(503), max_attempts=1)
    await _fail(client, 4)
    assert breaker.failure_count == 4

    hanging = _StubClient(
        rate=Rate.per_second(2), base_url=_URL, transport=_ScriptedTransport(hang=True), max_attempts=1
    )
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(hanging.get("x", expect=RESPONSE), timeout=0.05)

    assert breaker.failure_count == 4, "a cancelled call must not reset the failures already recorded"
    assert breaker.state is BreakerState.CLOSED

    await _fail(client, 1)
    assert breaker.state is BreakerState.OPEN, "the 5th real failure must still open the breaker"


async def test_only_one_request_probes_a_recovered_host(breaker: CircuitBreaker) -> None:
    """Half-open admits a single canary — not every caller that happened to be waiting."""
    from headgate.errors import ClientError

    transport = _ScriptedTransport(503)
    client = _StubClient(rate=Rate.per_second(2), base_url=_URL, transport=transport, max_attempts=1)
    await _fail(client, 5)
    assert breaker.state is BreakerState.OPEN
    calls_before = transport.calls

    await asyncio.sleep(0.06)  # cooldown elapses
    results = await asyncio.gather(*(client.get("x", expect=RESPONSE) for _ in range(3)), return_exceptions=True)

    assert transport.calls == calls_before + 1, "exactly one probe reaches the host"
    assert sum(isinstance(r, ClientError) for r in results) == 3


async def test_a_cancelled_probe_frees_the_slot(breaker: CircuitBreaker) -> None:
    """Otherwise the breaker stays half-open with a probe that will never report, forever."""
    failing = _StubClient(rate=Rate.per_second(2), base_url=_URL, transport=_ScriptedTransport(503), max_attempts=1)
    await _fail(failing, 5)
    await asyncio.sleep(0.06)

    hanging = _StubClient(
        rate=Rate.per_second(2), base_url=_URL, transport=_ScriptedTransport(hang=True), max_attempts=1
    )
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(hanging.get("x", expect=RESPONSE), timeout=0.05)  # this call takes the probe slot

    healthy = _ScriptedTransport(200)
    recovered = _StubClient(rate=Rate.per_second(2), base_url=_URL, transport=healthy, max_attempts=1)
    resp = await recovered.get("x", expect=RESPONSE)

    assert resp.status_code == 200, "the next caller must be admitted as the probe"
    assert healthy.calls == 1
    assert breaker.state is BreakerState.CLOSED


async def test_a_body_read_failure_counts_as_a_failure(breaker: CircuitBreaker) -> None:
    """The breaker must see the whole fetch, not just the headers.

    ``_send`` streams the response and caps the body afterwards, so a connection that dies
    mid-body raises AFTER any layer wrapped around the transport has already seen a 200.
    """
    from headgate.errors import TransportError

    client = _StubClient(rate=Rate.per_second(2), base_url=_URL, transport=_TruncatedBodyTransport(), max_attempts=1)

    with pytest.raises(TransportError):
        await client.get("x", expect=RESPONSE)

    assert breaker.failure_count == 1


async def test_a_retried_request_counts_one_failure(breaker: CircuitBreaker, monkeypatch: pytest.MonkeyPatch) -> None:
    """One fetch is one verdict, however many times the retry layer re-sent it.

    This is the property the old ``BreakerTransport`` was wrapped OUTSIDE the retry
    transport to get; it survives the move into ``_send`` because ``track()`` spans the
    whole fetch, retries included.
    """
    from headgate.errors import UpstreamStatusError

    monkeypatch.setattr("headgate._retry._BACKOFF_FACTOR", 0.0)
    transport = _ScriptedTransport(503)
    client = _StubClient(rate=Rate.per_second(2), base_url=_URL, transport=transport, max_attempts=3)

    with pytest.raises(UpstreamStatusError):
        await client.get("x", expect=RESPONSE)

    assert transport.calls == 3, "three attempts on the wire"
    assert breaker.failure_count == 1, "but one failure against the host"


class _RefusingAuth(httpx2.Auth):
    """An auth flow that fails to mint — for a CONFIGURATION reason the host never saw."""

    @override
    async def async_auth_flow(self, request: httpx2.Request) -> AsyncGenerator[httpx2.Request, httpx2.Response]:
        from headgate.errors import ClientConfigurationError

        raise ClientConfigurationError("rotated credential refused", source="test")
        yield request  # pragma: no cover - makes this an async generator


class _CachedHitTransport(httpx2.AsyncBaseTransport):
    """Serves what a cache HIT looks like to ``_send``: a 200 carrying hishel's from-cache marker."""

    def __init__(self) -> None:
        self.calls = 0

    @override
    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        self.calls += 1
        return httpx2.Response(200, content=b"{}", extensions={"hishel_from_cache": True})


async def test_an_auth_flow_failure_does_not_open_the_breaker_for_the_host(breaker: CircuitBreaker) -> None:
    """Five refused token mints used to open the breaker for EVERY client on the host."""
    from headgate.errors import ClientConfigurationError

    refusing = _StubClient(
        rate=Rate.per_second(2), base_url=_URL, transport=_ScriptedTransport(200), auth=_RefusingAuth(), max_attempts=1
    )
    for _ in range(6):
        with pytest.raises(ClientConfigurationError):
            await refusing.get("x", expect=RESPONSE)

    assert breaker.failure_count == 0
    healthy = _StubClient(rate=Rate.per_second(2), base_url=_URL, transport=_ScriptedTransport(200), max_attempts=1)
    assert (await healthy.get("x", expect=RESPONSE)).status_code == 200, (
        "a correctly configured client on the host is not refused"
    )


async def test_an_oversized_body_does_not_count_against_the_host(breaker: CircuitBreaker) -> None:
    """The size cap is our limit, not the host failing."""
    from headgate.errors import ResponseTooLargeError

    client = _StubClient(
        rate=Rate.per_second(2), base_url=_URL, transport=_ScriptedTransport(200), max_attempts=1, max_response_bytes=1
    )
    for _ in range(6):
        with pytest.raises(ResponseTooLargeError):
            await client.get("x", expect=RESPONSE)

    assert breaker.failure_count == 0
    assert breaker.state is BreakerState.CLOSED


async def test_a_cache_hit_neither_resets_failures_nor_closes_a_probe(breaker: CircuitBreaker) -> None:
    """A hit reached no network: it used to reset the failure count, and a probe served from cache closed the
    breaker without the host ever being contacted."""
    failing = _StubClient(rate=Rate.per_second(2), base_url=_URL, transport=_ScriptedTransport(503), max_attempts=1)
    cached = _StubClient(rate=Rate.per_second(2), base_url=_URL, transport=_CachedHitTransport(), max_attempts=1)

    await _fail(failing, 4)
    await cached.get("x", expect=RESPONSE)
    assert breaker.failure_count == 4, "the hit must not wipe the failures already recorded"

    await _fail(failing, 1)
    assert breaker.state is BreakerState.OPEN
    await asyncio.sleep(0.06)

    await cached.get("x", expect=RESPONSE)  # takes the probe slot, but tests nothing
    assert breaker.state is not BreakerState.CLOSED

    healthy = _ScriptedTransport(200)
    recovered = _StubClient(rate=Rate.per_second(2), base_url=_URL, transport=healthy, max_attempts=1)
    await recovered.get("x", expect=RESPONSE)
    assert healthy.calls == 1, "the next caller is admitted as the real probe"
    assert breaker.state is BreakerState.CLOSED


async def test_an_opted_out_client_is_not_refused_by_a_breaker_another_client_opened() -> None:
    """``circuit_breaker=False`` and policy-off skip the registry READ, not only the recording.

    The registry is process-global and keyed by host, so a client that merely stopped recording
    would still be refused by a breaker some other client opened on the same host. Runs on the
    registry's own 60s breaker rather than the ``breaker`` fixture: a 50ms cooldown could elapse
    mid-test and admit the control below as a probe.
    """
    from headgate.errors import TransportError

    HOST_BREAKERS.clear()
    try:
        await _fail(
            _StubClient(rate=Rate.per_second(2), base_url=_URL, transport=_ScriptedTransport(503), max_attempts=1), 5
        )
        assert HOST_BREAKERS[_HOST].state is BreakerState.OPEN

        refused = _ScriptedTransport(200)
        with pytest.raises(TransportError):
            await _StubClient(rate=Rate.per_second(2), base_url=_URL, transport=refused, max_attempts=1).get(
                "x", expect=RESPONSE
            )
        assert refused.calls == 0, "control: a client that consults the breaker is refused"

        no_breaker = _ScriptedTransport(200)
        await _StubClient(
            rate=Rate.per_second(2), base_url=_URL, transport=no_breaker, max_attempts=1, circuit_breaker=False
        ).get("x", expect=RESPONSE)
        policy_off = _ScriptedTransport(200)
        with policy_disabled():
            policy_off_client = _StubClient(
                rate=Rate.per_second(2), base_url=_URL, transport=policy_off, max_attempts=1
            )
        await policy_off_client.get("x", expect=RESPONSE)

        assert no_breaker.calls == 1, "circuit_breaker=False must reach the network"
        assert policy_off.calls == 1, "a policy-off client must reach the network"
    finally:
        HOST_BREAKERS.clear()
