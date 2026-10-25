# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""``max_in_flight``: a concurrency cap shared across loops and threads, held from the send until the body closes.

The cap counts a request until its response body is closed, because the server is still serving a request whose body
is streaming; so the tests close (or read) responses to free slots, and check every path that drops a response or
fails a send gives its slot back, cancellation included.
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import TYPE_CHECKING, override

import httpx2
import pytest

from headgate import HttpOptions
from headgate.ratelimit import PathBudgets, Rate, RateBudget, RateLimitTransport
from headgate.ratelimit._inflight import InFlightLimit
from tests.support.clients import StubClient
from tests.support.responses import RESPONSE

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

#: Fast enough never to pace in a test.
_INSTANT = Rate.per_second(1_000_000)

#: How long the cross-thread test waits for the other thread, and how often it looks.
_THREAD_TIMEOUT = 5.0
_POLL = 0.001

#: How many concurrent calls a capped client serves in the end-to-end test.
_CALLS = 6


class _Chunks(httpx2.AsyncByteStream):
    """A body of two chunks that records whether it was closed."""

    def __init__(self) -> None:
        """Start open."""
        self.closed = False

    @override
    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield b"a"
        yield b"b"

    @override
    async def aclose(self) -> None:
        self.closed = True


class _SyncBody(httpx2.SyncByteStream):
    """A synchronous body, which only a synchronous caller could close."""

    @override
    def __iter__(self) -> Iterator[bytes]:
        yield b"x"


class _Wire(httpx2.AsyncBaseTransport):
    """Answer with a fresh streaming body, a synchronous one, or raise, and count what reached it."""

    def __init__(self, *, fail: bool = False, sync: bool = False) -> None:
        """Answer with a streaming body, unless *fail* (raise ``ConnectError``) or *sync* (a synchronous body)."""
        self._fail = fail
        self._sync = sync
        self.sent = 0

    @override
    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        self.sent += 1
        if self._fail:
            msg = "refused"
            raise httpx2.ConnectError(msg, request=request)
        if self._sync:
            return httpx2.Response(200, stream=_SyncBody())
        return httpx2.Response(200, stream=_Chunks())


def _layer(budget: RateBudget, wire: httpx2.AsyncBaseTransport) -> RateLimitTransport:
    """Return the rate-limit layer spending *budget* over *wire*."""
    return RateLimitTransport(budgets=PathBudgets(default=budget), transport=wire)


def _request() -> httpx2.Request:
    """Return a request to send through the layer."""
    return httpx2.Request("GET", "https://x/")


# --- The limit itself ---


async def test_a_freed_slot_goes_to_the_oldest_waiter() -> None:
    """Admit up to the limit, queue the rest, and hand each freed slot to the longest waiter."""
    limit = InFlightLimit(1)
    await limit.acquire()
    order: list[int] = []

    async def take(n: int) -> None:
        await limit.acquire()
        order.append(n)

    waiters = [asyncio.create_task(take(n)) for n in range(2)]
    await asyncio.sleep(0)
    assert (limit.limit, limit.in_flight, order) == (1, 1, [])
    limit.release()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    limit.release()
    await asyncio.gather(*waiters)
    limit.release()
    assert (order, limit.in_flight) == ([0, 1], 0)


async def test_a_waiter_cancelled_before_its_turn_leaves_the_queue() -> None:
    """Drop a cancelled waiter from the queue, so a later release frees the slot instead of handing it to nobody."""
    limit = InFlightLimit(1)
    await limit.acquire()
    waiter = asyncio.create_task(limit.acquire())
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    limit.release()
    assert limit.in_flight == 0


async def test_a_waiter_cancelled_while_its_slot_is_on_the_way_passes_it_on() -> None:
    """Give the slot back when the waiter is cancelled after it was handed over but before it resumed."""
    limit = InFlightLimit(1)
    await limit.acquire()
    waiter = asyncio.create_task(limit.acquire())
    await asyncio.sleep(0)
    limit.release()  # schedules the handover on the waiter's loop
    waiter.cancel()  # lands before the handover runs
    with pytest.raises(asyncio.CancelledError):
        await waiter
    await asyncio.sleep(0)
    assert limit.in_flight == 0


async def test_a_waiter_cancelled_after_its_slot_arrived_gives_it_back() -> None:
    """Release the slot a waiter received when cancellation reaches it before it resumes."""
    limit = InFlightLimit(1)
    await limit.acquire()
    waiter = asyncio.create_task(limit.acquire())
    await asyncio.sleep(0)
    limit.release()
    # Runs after the handover sets the waiter's result, before the waiter itself resumes.
    asyncio.get_running_loop().call_soon(waiter.cancel)
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert limit.in_flight == 0


def test_a_slot_freed_for_a_waiter_on_a_closed_loop_goes_to_the_pool() -> None:
    """Skip a waiter whose event loop has closed, since it can never resume to use the slot."""
    limit = InFlightLimit(1)
    asyncio.run(limit.acquire())
    orphaned = asyncio.new_event_loop()
    try:
        parked = orphaned.create_task(limit.acquire())
        orphaned.run_until_complete(asyncio.sleep(0))
        assert not parked.done()
    finally:
        orphaned.close()
    limit.release()
    assert limit.in_flight == 0


def test_a_slot_is_handed_across_threads_and_loops() -> None:
    """Wake a waiter on another thread's loop when this thread frees the slot."""
    limit = InFlightLimit(1)
    asyncio.run(limit.acquire())
    acquired = threading.Event()

    def wait_in_another_loop() -> None:
        asyncio.run(limit.acquire())
        acquired.set()

    thread = threading.Thread(target=wait_in_another_loop)
    thread.start()
    deadline = time.monotonic() + _THREAD_TIMEOUT
    while not limit.waiting and time.monotonic() < deadline:
        time.sleep(_POLL)
    assert limit.waiting == 1, "the other thread is parked on the limit"
    limit.release()
    thread.join(timeout=_THREAD_TIMEOUT)
    assert acquired.is_set()
    assert limit.in_flight == 1


# --- The slot travels with the response ---


async def test_the_slot_is_held_until_the_body_is_closed() -> None:
    """Keep the second request waiting until the first response's body is closed, not when its head arrives."""
    budget = RateBudget(_INSTANT, max_in_flight=1)
    wire = _Wire()
    layer = _layer(budget, wire)

    first = await layer.handle_async_request(_request())
    second = asyncio.create_task(layer.handle_async_request(_request()))
    await asyncio.sleep(0)
    assert (wire.sent, budget.in_flight) == (1, 1), "the second send waits while the first body is open"

    assert [chunk async for chunk in first.aiter_raw()] == [b"a", b"b"]
    response = await second
    assert wire.sent == len((first, response))
    await response.aclose()
    await response.aclose()
    assert budget.in_flight == 0, "closing twice frees one slot"


async def test_a_failed_send_gives_its_slot_back() -> None:
    """Free the slot when the send raises, so a failing upstream cannot exhaust the cap."""
    budget = RateBudget(_INSTANT, max_in_flight=1)
    layer = _layer(budget, _Wire(fail=True))
    for _ in range(2):
        with pytest.raises(httpx2.ConnectError):
            await layer.handle_async_request(_request())
    assert budget.in_flight == 0


async def test_a_synchronous_body_does_not_hold_a_slot() -> None:
    """Free the slot at once for a body an async caller cannot close."""
    budget = RateBudget(_INSTANT, max_in_flight=1)
    response = await _layer(budget, _Wire(sync=True)).handle_async_request(_request())
    assert budget.in_flight == 0
    assert isinstance(response.stream, _SyncBody)


async def test_a_capped_client_frees_every_slot_it_reads() -> None:
    """Serve more concurrent calls than the cap through a client: each buffered read frees its slot."""
    budget = RateBudget(_INSTANT, max_in_flight=2)
    client = StubClient(base_url="https://x", rate=budget, http=HttpOptions(transport=_Wire()))
    responses = await asyncio.gather(*(client.get("y", expect=RESPONSE) for _ in range(_CALLS)))
    assert [response.content for response in responses] == [b"ab"] * _CALLS
    assert budget.in_flight == 0
