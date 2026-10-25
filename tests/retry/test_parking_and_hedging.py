# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""What the retry layer keeps of a failure it waits after, and the exact bounds of its hedges and waits.

A failed response is parked through the wait: a bounded, decoded copy held in memory, its connection released. When
the sequence stops after the wait (the breaker opened meanwhile, here), that copy is what the call answers with, so it
must keep the status, the headers that still describe it, the body read and the extensions. The boundaries (the last
attempt a hedge may take, a ``Retry-After`` exactly at the cap, a deadline leaving exactly ``min_attempt_time``) are
pinned on a :class:`~headgate.testing.TestClock`.
"""

from __future__ import annotations

import asyncio
import gzip
from typing import TYPE_CHECKING, Final, override

import httpx2
import pytest

from headgate._core import BREAKER_GATE, CallContext, CallInfo, Emitter, RetryScheduled
from headgate.retry import Backoff, Hedge, RetryBudget, RetryPolicy, RetryTransport
from headgate.testing import FaultyTransport, Respond, TestClock
from headgate.testing.faults import head_size

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from headgate._core import Event

#: The most of a failed response's body the retry layer keeps through a wait.
_PARKED_BODY_BYTES: Final = 64 * 1024
#: A marker extension a parked copy must keep.
_MARK: Final = "test.mark"
_REQUEST_ID: Final = "req-1"
#: Waits a quarter-second backoff exactly before the first retry (``0.125 · 2¹``).
_QUARTER: Final = Backoff(factor=0.125, jitter=lambda: 1.0)
_HEDGE_AFTER: Final = 0.5


def _request(clock: TestClock, *, deadline: float | None = None) -> tuple[httpx2.Request, CallContext]:
    """Build a GET with a body and a header, and its context on *clock*."""
    request = httpx2.Request("GET", "https://api.test/orders", headers={"X-Trace": "t-1"}, content=b"query")
    context = CallContext(CallInfo(method="GET", url="orders"), clock=clock, deadline_after=deadline)
    context.attach(request)
    return request, context


class _Body(httpx2.AsyncByteStream):
    """A response body of *chunks* that counts how many were pulled, and can fail mid-way or on close."""

    def __init__(self, chunks: list[bytes], *, fail_after: int | None = None, close_fails: bool = False) -> None:
        """Stream *chunks*; raise ``ReadError`` after *fail_after* of them, or from ``aclose`` when *close_fails*."""
        self._chunks = chunks
        self._fail_after = fail_after
        self._close_fails = close_fails
        self.pulled = 0
        self.closed = False

    @override
    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self._chunks:
            if self.pulled == self._fail_after:
                msg = "connection dropped mid-body"
                raise httpx2.ReadError(msg)
            self.pulled += 1
            yield chunk

    @override
    async def aclose(self) -> None:
        self.closed = True
        if self._close_fails:
            msg = "connection reset while closing"
            raise httpx2.ReadError(msg)


class _FailsThenAnswers(httpx2.AsyncBaseTransport):
    """Answer the first send with a 503 over *body*, and every later one with a 200."""

    def __init__(self, body: _Body, headers: dict[str, str] | None = None) -> None:
        """Fail first over *body* with *headers*."""
        self.body = body
        self._headers = {"x-request-id": _REQUEST_ID, **(headers or {})}
        self.sends = 0

    @override
    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        self.sends += 1
        if self.sends > 1:
            return httpx2.Response(200, request=request)
        return httpx2.Response(503, headers=self._headers, stream=self.body, extensions={_MARK: "kept"})


async def _parked(network: httpx2.AsyncBaseTransport) -> httpx2.Response:
    """Send a GET whose breaker opens during the wait after its first attempt, and return what it answers with."""
    clock = TestClock()
    opened = False

    async def open_breaker(seconds: float, /) -> None:
        """Open the breaker, then wait."""
        nonlocal opened
        opened = True
        await clock.sleep(seconds)

    retry = RetryTransport(transport=network, policy=RetryPolicy(budget=None), sleep=open_breaker, clock=clock)
    request, context = _request(clock)
    context.put(BREAKER_GATE, lambda: opened)
    return await retry.handle_async_request(request)


async def test_a_parked_failure_keeps_its_status_headers_extensions_and_body() -> None:
    """Answer with the parked 503 whole: its request id, its extensions, and its body joined from every chunk."""
    network = _FailsThenAnswers(_Body([b"first ", b"second"]))

    response = await _parked(network)

    assert (response.status_code, response.headers["x-request-id"]) == (503, _REQUEST_ID)
    assert response.extensions[_MARK] == "kept"
    assert response.content == b"first second"
    assert network.body.closed
    assert network.sends == 1


async def test_a_parked_failure_is_decoded_and_drops_the_wire_encoding_headers() -> None:
    """Park a gzip body decoded, without the headers that described its encoding on the wire."""
    compressed = gzip.compress(b"maintenance window")
    body = _Body([compressed[:5], compressed[5:]])
    network = _FailsThenAnswers(body, {"content-encoding": "gzip", "content-length": str(len(compressed))})

    response = await _parked(network)

    assert response.content == b"maintenance window"
    assert "content-encoding" not in response.headers
    assert response.headers["content-length"] == str(len(b"maintenance window"))
    assert response.headers["x-request-id"] == _REQUEST_ID


async def test_a_parked_body_is_read_only_up_to_its_bound() -> None:
    """Stop reading a failure's body once the bound is reached: exactly at it, not one chunk later or sooner."""
    chunks = [b"x" * (_PARKED_BODY_BYTES - 1), b"y", b"z" * 10]
    network = _FailsThenAnswers(_Body(chunks))

    response = await _parked(network)

    assert network.body.pulled == len(("almost the bound", "the byte reaching it"))
    assert response.content == b"x" * (_PARKED_BODY_BYTES - 1) + b"y"


async def test_a_parked_body_of_many_small_chunks_is_bounded_by_their_sum() -> None:
    """Count every chunk toward the bound, so a body of many small chunks is cut too."""
    chunk = b"c" * 1024
    network = _FailsThenAnswers(_Body([chunk] * 100))

    response = await _parked(network)

    assert network.body.pulled == _PARKED_BODY_BYTES // len(chunk)
    assert len(response.content) == _PARKED_BODY_BYTES


async def test_a_failure_whose_body_drops_mid_way_is_parked_with_what_arrived() -> None:
    """Park what arrived of a body whose connection dropped, rather than fail the call on it."""
    network = _FailsThenAnswers(_Body([b"part", b"never"], fail_after=1))

    response = await _parked(network)

    assert (response.status_code, response.content) == (503, b"part")


async def test_a_failure_whose_close_fails_is_parked_all_the_same() -> None:
    """Park a failure whose connection fails to close once its bounded head is read."""
    network = _FailsThenAnswers(_Body([b"x" * _PARKED_BODY_BYTES, b"more"], close_fails=True))

    response = await _parked(network)

    assert response.status_code == httpx2.codes.SERVICE_UNAVAILABLE
    assert network.body.closed


# --- Hedges ---


class _Changes:
    """An event set whenever a test double records something."""

    def __init__(self) -> None:
        """Start with nothing recorded."""
        self._event = asyncio.Event()

    def note(self) -> None:
        """Wake whoever waits."""
        self._event.set()

    async def until(self, condition: Callable[[], object]) -> None:
        """Wait until *condition* holds."""
        while not condition():
            self._event.clear()
            await self._event.wait()


class _Gate:
    """A sleep that waits for the test, recording each wait and each cancellation."""

    def __init__(self, changes: _Changes) -> None:
        """Start closed."""
        self._changes = changes
        self.opened = asyncio.Event()
        self.waits: list[float] = []
        self.cancelled = 0

    async def sleep(self, seconds: float, /) -> None:
        """Record *seconds* and wait for the gate."""
        self.waits.append(seconds)
        self._changes.note()
        try:
            await self.opened.wait()
        except asyncio.CancelledError:
            self.cancelled += 1
            raise


class _Held(httpx2.AsyncBaseTransport):
    """A network whose sends each wait for their own release, keeping every request sent."""

    def __init__(self, changes: _Changes) -> None:
        """Start with no sends."""
        self._changes = changes
        self.started: list[asyncio.Future[int]] = []
        self.requests: list[tuple[httpx2.Headers, bytes]] = []

    @override
    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        answer = asyncio.get_running_loop().create_future()
        self.started.append(answer)
        self.requests.append((request.headers, await request.aread()))
        self._changes.note()
        return httpx2.Response(await answer, request=request, stream=_Body([b"answer"]))


def _held(policy: RetryPolicy) -> tuple[RetryTransport, _Held, _Gate, _Changes]:
    """Return a retry transport under *policy* over a held network and a gated hedge timer."""
    changes = _Changes()
    network, gate = _Held(changes), _Gate(changes)
    return RetryTransport(transport=network, policy=policy, sleep=gate.sleep), network, gate, changes


async def _settle() -> None:
    """Let every task that can run do so."""
    for _ in range(20):
        await asyncio.sleep(0)


@pytest.mark.parametrize(("attempts", "max_extra", "sends"), [(2, 3, 2), (6, 2, 3)], ids=["attempts", "max-extra"])
async def test_hedges_stop_at_the_attempts_and_at_max_extra(attempts: int, max_extra: int, sends: int) -> None:
    """Start hedges until the sends reach ``attempts`` or the hedges reach ``max_extra``, whichever comes first."""
    policy = RetryPolicy(attempts=attempts, budget=None, hedge=Hedge(after=_HEDGE_AFTER, max_extra=max_extra))
    retry, network, gate, changes = _held(policy)
    request, _ = _request(TestClock())

    call = asyncio.create_task(retry.handle_async_request(request))
    await changes.until(lambda: network.started and gate.waits)
    gate.opened.set()
    await _settle()

    assert len(network.started) == sends
    network.started[0].set_result(200)
    response = await call
    assert response.status_code == httpx2.codes.OK


async def test_a_hedge_is_a_copy_of_the_request_with_its_headers_and_body() -> None:
    """Send the hedge with the request's own headers and body, and hand back the winner open and readable."""
    retry, network, gate, changes = _held(RetryPolicy(budget=None, hedge=Hedge(after=_HEDGE_AFTER)))
    request, _ = _request(TestClock())

    call = asyncio.create_task(retry.handle_async_request(request))
    await changes.until(lambda: network.started and gate.waits)
    gate.opened.set()
    await changes.until(lambda: len(network.started) == len(("first", "hedge")))
    network.started[1].set_result(200)
    response = await call

    (first_headers, first_body), (hedge_headers, hedge_body) = network.requests
    assert (hedge_headers["x-trace"], hedge_body) == (first_headers["x-trace"], first_body) == ("t-1", b"query")
    assert not response.is_closed
    assert await response.aread() == b"answer"


async def test_a_retryable_failure_in_a_hedged_round_waits_for_the_other_send() -> None:
    """Keep the round open while another send is in flight: no backoff, no new attempt, until it answers."""
    retry, network, gate, changes = _held(RetryPolicy(budget=None, hedge=Hedge(after=_HEDGE_AFTER)))
    request, _ = _request(TestClock())

    call = asyncio.create_task(retry.handle_async_request(request))
    await changes.until(lambda: network.started and gate.waits)
    gate.opened.set()
    await changes.until(lambda: len(network.started) == len(("first", "hedge")))
    network.started[0].set_result(503)
    await _settle()

    assert not call.done()
    assert len(network.started) == len(("first", "hedge"))
    network.started[1].set_result(200)
    assert (await call).status_code == httpx2.codes.OK


@pytest.mark.parametrize(
    "policy",
    [
        RetryPolicy(hedge=Hedge(after=_HEDGE_AFTER), budget=RetryBudget(ratio=0.0, min_per_second=0.0)),
        RetryPolicy(hedge=Hedge(after=_HEDGE_AFTER), budget=None, min_attempt_time=2.0),
    ],
    ids=["no-budget", "no-time"],
)
async def test_a_refused_hedge_leaves_the_first_send_to_answer(policy: RetryPolicy) -> None:
    """Start no hedge when the budget or the deadline refuses one, and keep waiting on the first send."""
    retry, network, gate, changes = _held(policy)
    request, _ = _request(TestClock(), deadline=1.0)

    call = asyncio.create_task(retry.handle_async_request(request))
    await changes.until(lambda: network.started and gate.waits)
    gate.opened.set()
    await _settle()

    assert len(network.started) == 1
    network.started[0].set_result(200)
    assert (await call).status_code == httpx2.codes.OK
    assert len(network.started) == 1


async def test_an_answer_before_the_hedge_delay_cancels_the_hedge_timer() -> None:
    """Cancel the hedge timer still waiting when the first send answers."""
    retry, network, gate, changes = _held(RetryPolicy(budget=None, hedge=Hedge(after=_HEDGE_AFTER)))
    request, _ = _request(TestClock())

    call = asyncio.create_task(retry.handle_async_request(request))
    await changes.until(lambda: network.started and gate.waits)
    network.started[0].set_result(200)
    await call

    assert gate.cancelled == 1


# --- Exact bounds of a wait ---


async def test_a_retry_after_exactly_at_the_cap_is_waited() -> None:
    """Wait a ``Retry-After`` equal to ``max_retry_after``; only a longer one stops the call."""
    clock = TestClock()
    network = FaultyTransport(schedule=[Respond(503, retry_after=2.0), Respond(200)], sleep=clock.sleep)
    policy = RetryPolicy(budget=None, max_retry_after=2.0)
    retry = RetryTransport(transport=network, policy=policy, sleep=clock.sleep, clock=clock)
    request, _ = _request(clock)

    response = await retry.handle_async_request(request)

    assert response.status_code == httpx2.codes.OK
    assert clock.sleeps == [2.0]
    await response.aclose()


async def test_a_retry_leaving_exactly_min_attempt_time_is_made() -> None:
    """Retry when the wait leaves exactly ``min_attempt_time`` before the deadline, both before and after it."""
    clock = TestClock()
    network = FaultyTransport(schedule=[Respond(503), Respond(200)], sleep=clock.sleep)
    policy = RetryPolicy(budget=None, backoff=_QUARTER, min_attempt_time=0.5)
    retry = RetryTransport(transport=network, policy=policy, sleep=clock.sleep, clock=clock)
    request, _ = _request(clock, deadline=0.75)

    response = await retry.handle_async_request(request)

    assert response.status_code == httpx2.codes.OK
    assert clock.sleeps == [_QUARTER.delay(1)]
    await response.aclose()


def test_the_head_size_counts_the_request_line_and_every_header_line() -> None:
    """Count the head exactly as HTTP/1.1 writes it, each line with its separator and CRLF."""
    request = httpx2.Request("GET", "https://api.test/orders?id=1", headers={"X-Trace": "t-1"})

    wire = b"GET /orders?id=1 HTTP/1.1\r\nHost: api.test\r\nX-Trace: t-1\r\n"
    assert head_size(request) == len(wire)


class _Events:
    """An observer that keeps every event."""

    def __init__(self) -> None:
        """Start with no events."""
        self.events: list[Event] = []

    def notify(self, event: Event) -> None:
        """Keep *event*."""
        self.events.append(event)


async def test_a_status_the_server_says_to_retry_keeps_its_retry_after_and_is_named() -> None:
    """Judge a 503 marked ``x-should-retry: true`` with its ``Retry-After``, naming the status as the reason."""
    clock = TestClock()
    marked = Respond(503, headers={"x-should-retry": "true"}, retry_after=2.0)
    network = FaultyTransport(schedule=[marked, Respond(200)], sleep=clock.sleep)
    retry = RetryTransport(transport=network, policy=RetryPolicy(budget=None), sleep=clock.sleep, clock=clock)
    observer = _Events()
    request = httpx2.Request("GET", "https://api.test/orders")
    context = CallContext(CallInfo(method="GET", url="orders"), clock=clock, emitter=Emitter((observer,)))
    context.attach(request)

    response = await retry.handle_async_request(request)

    assert response.status_code == httpx2.codes.OK
    assert context.notes[0] == "attempt 1/3: 503 Service Unavailable (Retry-After 2.0s) after 0.0s"
    scheduled = [event for event in observer.events if isinstance(event, RetryScheduled)]
    assert [(event.reason, event.delay) for event in scheduled] == [("503", 2.0)]
    await response.aclose()
