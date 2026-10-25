# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""``headgate.retry.RetryTransport`` reproduces httpx-retries 0.6.0 exactly, case for case.

``tests/support/retry_golden.json`` holds httpx-retries' outcome for every case of the retry matrix
(``tests/support/retry_matrix.py``): the sends, every sleep as an exact float, the result, the attempt count, and the
closes, recorded with the library configured as the default ``RetryPolicy`` configures this transport. These tests
replay every case against ``RetryTransport``.

It is an oracle, not a snapshot, so it is deliberately not syrupy: the table is an external library's output, and
``--snapshot-update`` would re-capture it from the transport under test, the one thing it must never be.
"""

from __future__ import annotations

import asyncio
import threading
from typing import TYPE_CHECKING, override

import httpx2

from headgate.retry import Backoff, RetryPolicy, RetryTransport
from tests.support.retry_matrix import CASES, case_json, load_golden, outcome_json, run_case

if TYPE_CHECKING:
    from collections.abc import Callable

#: How many cases the cross-thread test replays: enough to cover sleeps of every kind, few enough to stay fast.
_CROSS_THREAD_CASES = 100

#: How long the cross-thread rendezvous waits for the background loop before failing the case.
_RENDEZVOUS_TIMEOUT = 5.0


def _in_house(
    inner: httpx2.AsyncBaseTransport, attempts: int, jitter: Callable[[], float]
) -> httpx2.AsyncBaseTransport:
    """Wrap *inner* in the transport under test, configured as production configures it.

    The matrix's attempt budgets include 0, which httpx-retries clamped to one send; a ``RetryPolicy`` refuses it, so
    it is clamped here the same way.
    """
    policy = RetryPolicy(attempts=max(attempts, 1), backoff=Backoff(jitter=jitter))
    return RetryTransport(transport=inner, policy=policy)


def test_the_golden_table_covers_the_matrix() -> None:
    """Pin that the table's rows are the matrix's cases, in order, so every case has its recorded outcome."""
    assert [row["case"] for row in load_golden()] == [case_json(case) for case in CASES]


async def test_the_in_house_transport_reproduces_the_golden_table() -> None:
    """Replay every case and compare the observed outcome with the recorded one."""
    golden = load_golden()
    mismatches = []
    for case, row in zip(CASES, golden, strict=True):
        got = outcome_json(await run_case(_in_house, case))
        if got != row["outcome"]:
            mismatches.append((row["case"], row["outcome"], got))
    assert not mismatches, f"{len(mismatches)} of {len(golden)} cases differ; first three: {mismatches[:3]}"


class _Rendezvous(httpx2.AsyncBaseTransport):
    """Hold each send until the background loop has begun and finished a sleep while this case is in flight.

    So every case overlaps a foreign sleep by construction, rather than by the luck of thread scheduling. The wait
    blocks this loop's thread, which is the point: the case cannot move on until the other thread has slept.
    """

    def __init__(self, inner: httpx2.AsyncBaseTransport, in_case: threading.Event, slept: threading.Event) -> None:
        """Wrap *inner*; *in_case* is set while a send is held, *slept* once the background loop slept during it."""
        self._inner = inner
        self._in_case = in_case
        self._slept = slept

    @override
    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        self._slept.clear()
        self._in_case.set()
        overlapped = self._slept.wait(timeout=_RENDEZVOUS_TIMEOUT)
        self._in_case.clear()
        assert overlapped, "the background loop did not sleep during the case"
        return await self._inner.handle_async_request(request)


async def test_a_coroutine_sleeping_on_another_threads_loop_is_not_recorded_into_a_case() -> None:
    """Record only the case's own sleeps, though the recorder patches ``asyncio.sleep`` process-wide.

    The patch reaches every thread's event loop, so a loop running on another thread, handed a sleep that returns at
    once, would spin for the whole case and flood it with foreign sleeps. Every send here waits until such a
    background loop has slept inside it.
    """
    stop, in_case, slept = threading.Event(), threading.Event(), threading.Event()

    async def background() -> None:
        while not stop.is_set():
            began_in_case = in_case.is_set()  # read before the sleep, so the sleep began inside the case
            await asyncio.sleep(0)
            if began_in_case:
                slept.set()

    def with_rendezvous(
        inner: httpx2.AsyncBaseTransport, attempts: int, jitter: Callable[[], float]
    ) -> httpx2.AsyncBaseTransport:
        return _in_house(_Rendezvous(inner, in_case, slept), attempts, jitter)

    thread = threading.Thread(target=asyncio.run, args=(background(),), daemon=True)
    thread.start()
    try:
        mismatches = [
            row["case"]
            for case, row in list(zip(CASES, load_golden(), strict=True))[:_CROSS_THREAD_CASES]
            if outcome_json(await run_case(with_rendezvous, case)) != row["outcome"]
        ]
    finally:
        stop.set()
        thread.join(timeout=_RENDEZVOUS_TIMEOUT)
    assert not mismatches, f"{len(mismatches)} cases recorded another thread's sleeps; first three: {mismatches[:3]}"
