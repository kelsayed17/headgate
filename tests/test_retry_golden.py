"""The in-house retry transport reproduces httpx-retries 0.6.0 exactly. The pin outlives the library.

``support/retry_golden.json`` holds httpx-retries' outcome for every case of the retry matrix
(``support/retry_matrix.py``): the sends, every sleep as an exact float, the result, the attempt count, and the
closes. It was generated from the library, configured as ``build_retry`` configured it, while the library was
installed (spec §8, the "golden table"). This replays every case against ``headgate._transports.RetryTransport``.

An ORACLE, not a snapshot, so it is deliberately not syrupy: the table is an external library's output,
and ``--snapshot-update`` would re-capture it from the in-house transport under test, the one thing it must never be.
"""

from __future__ import annotations

import asyncio
import threading
from typing import TYPE_CHECKING, override

import httpx2

from headgate import _transports
from headgate._transports import RetryTransport, build_retry
from tests.support.retry_matrix import CASES, case_json, load_golden, outcome_json, run_case

if TYPE_CHECKING:
    import pytest

#: Read at import, before the autouse ``no_retry_backoff`` fixture zeroes it, because the table holds production
#: sleeps.
_PRODUCTION_BACKOFF_FACTOR = _transports._BACKOFF_FACTOR


def _in_house(inner: httpx2.AsyncBaseTransport, attempts: int) -> httpx2.AsyncBaseTransport:
    return RetryTransport(transport=inner, retry=build_retry(attempts))


def test_the_production_backoff_was_captured_before_the_autouse_fixture_zeroed_it() -> None:
    assert _PRODUCTION_BACKOFF_FACTOR > 0, "captured after no_retry_backoff ran — the table holds production sleeps"


def test_the_golden_table_covers_the_matrix() -> None:
    assert [row["case"] for row in load_golden()] == [case_json(case) for case in CASES]


async def test_the_in_house_transport_reproduces_the_golden_table(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_transports, "_BACKOFF_FACTOR", _PRODUCTION_BACKOFF_FACTOR)
    golden = load_golden()
    mismatches = []
    for case, row in zip(CASES, golden, strict=True):
        got = outcome_json(await run_case(_in_house, case))
        if got != row["outcome"]:
            mismatches.append((row["case"], row["outcome"], got))
    assert not mismatches, f"{len(mismatches)} of {len(golden)} cases differ; first three: {mismatches[:3]}"


class _Rendezvous(httpx2.AsyncBaseTransport):
    """Holds each send until the background loop has begun AND finished a sleep while this case is in flight.

    So every case overlaps a foreign sleep by construction, rather than by the luck of thread scheduling. The wait
    blocks this loop's thread, which is the point: the case cannot move on until the other thread has slept.
    """

    def __init__(self, inner: httpx2.AsyncBaseTransport, in_case: threading.Event, slept: threading.Event) -> None:
        self._inner = inner
        self._in_case = in_case
        self._slept = slept

    @override
    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        self._slept.clear()
        self._in_case.set()
        overlapped = self._slept.wait(timeout=5)
        self._in_case.clear()
        assert overlapped, "the background loop did not sleep during the case"
        return await self._inner.handle_async_request(request)


async def test_a_coroutine_sleeping_on_another_threads_loop_is_not_recorded_into_a_case(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The recorder patches ``asyncio.sleep`` for the whole process, so it reaches every thread's event loop.

    A background loop left running by another test (DBOS keeps one) once wrote 771 sleeps of 10.0 s into one case
    and failed the table: handed a sleep that returns at once, its loop spun for the whole case. Every send here waits
    until the background loop has slept inside it; only the case's own sleeps may be recorded.
    """
    monkeypatch.setattr(_transports, "_BACKOFF_FACTOR", _PRODUCTION_BACKOFF_FACTOR)
    stop, in_case, slept = threading.Event(), threading.Event(), threading.Event()

    async def background() -> None:
        while not stop.is_set():
            began_in_case = in_case.is_set()  # read BEFORE the sleep, so the sleep began inside the case
            await asyncio.sleep(0)
            if began_in_case:
                slept.set()

    def with_rendezvous(inner: httpx2.AsyncBaseTransport, attempts: int) -> httpx2.AsyncBaseTransport:
        return _in_house(_Rendezvous(inner, in_case, slept), attempts)

    thread = threading.Thread(target=asyncio.run, args=(background(),), daemon=True)
    thread.start()
    try:
        mismatches = [
            row["case"]
            for case, row in list(zip(CASES, load_golden(), strict=True))[:100]
            if outcome_json(await run_case(with_rendezvous, case)) != row["outcome"]
        ]
    finally:
        stop.set()
        thread.join(timeout=5)
    assert not mismatches, f"{len(mismatches)} cases recorded another thread's sleeps; first three: {mismatches[:3]}"
