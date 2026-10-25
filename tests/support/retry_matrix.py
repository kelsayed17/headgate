# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""The retry matrix: every input the retry transport decides on, and the golden outcome of each.

``headgate.retry.RetryTransport`` reproduces httpx-retries 0.6.0, as the default ``RetryPolicy`` configures it,
exactly. This module is the matrix httpx-retries was driven through, once, to write the golden table, and that
headgate's transport is driven through on every run: attempt budget x method x status x exception x every
``Retry-After`` form. The golden table beside it (``retry_golden.json``) holds httpx-retries' own outcome for every
case, and ``tests/retry/test_retry_golden.py`` replays it.

An outcome is everything a caller of the transport can observe:

* how many sends reached the network;
* every sleep, as an exact float (the clock is frozen, and the jitter is the ``random`` module's generator seeded with
  the case's seed, which is what httpx-retries drew from when the table was recorded);
* what came back: a status, or the exception's class name;
* the retries spent on a returned response, read from the call context's attempt count (``None`` for a method the
  layer never retries, which httpx-retries left unmarked);
* which network responses were closed. Each is backed by a stream httpx2 does not pre-read, so ``is_closed`` flips only
  when the retry layer calls ``aclose()``: a response it retried past reads ``true``, the one it returned ``false``;
* which were already closed when each sleep began, so closing the failed response before sleeping is pinned, and not
  only that it is closed by the end.

Provenance, and changing the table. The table was generated from httpx-retries 0.6.0, configured as the default
``RetryPolicy`` configures this transport, by a one-off differential script that is not part of this tree. A change to
the table is a deliberate behaviour change (for example to the 5 s ``Retry-After`` clamp, the broad exception tuple, or
retrying 501): it is made on purpose, with the row diff reviewed, and this statement is updated in the same commit.
Never regenerate the table from headgate's own transport to make a failing replay pass: it would then stop being
evidence of anything.
"""

from __future__ import annotations

import asyncio
import json
import random
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from typing import TYPE_CHECKING, override
from unittest.mock import patch

import httpx2
import msgspec
import time_machine

from headgate._core import CallContext
from headgate.retry._policy import RETRIED_METHODS

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Iterator

#: Where the golden outcomes live, one JSON row per case.
GOLDEN = Path(__file__).with_name("retry_golden.json")

#: The instant every case runs at, so an HTTP-date ``Retry-After`` resolves to the same delay on every run.
FROZEN_NOW = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)

#: Wraps the scripted network transport in a retry transport allowed ``attempts`` sends that draws its jitter from the
#: given source.
type TransportFactory = Callable[[httpx2.AsyncBaseTransport, int, Callable[[], float]], httpx2.AsyncBaseTransport]


@dataclass(frozen=True, slots=True)
class Step:
    """One network outcome: a status (with an optional ``Retry-After``) or a raised exception, named by class."""

    status: int | None = None
    raises: str | None = None
    retry_after: str | None = None


@dataclass(frozen=True, slots=True)
class Case:
    """One matrix case: ``attempts`` is the send budget; the script's last step repeats once it runs out."""

    attempts: int
    method: str
    script: tuple[Step, ...]
    seed: int = 0


@dataclass(frozen=True, slots=True)
class Outcome:
    """What the caller of the retry transport observed for one case."""

    sends: int
    sleeps: tuple[float, ...]
    result: str
    attempts_made: int | None
    closed: tuple[bool, ...]
    closed_at_sleep: tuple[tuple[bool, ...], ...]


class _UnreadStream(httpx2.AsyncByteStream):
    """A response body httpx2 leaves unread, so the response's ``is_closed`` reports whether anything closed it.

    ``httpx2.ByteStream`` is read, and so closed, at construction; this stream is not.
    """

    @override
    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield b""

    @override
    async def aclose(self) -> None:
        pass


#: Spelled out, because ``strftime`` names days and months in the process locale.
_WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _http_date(offset_seconds: int) -> str:
    """Return the IMF-fixdate *offset_seconds* after ``FROZEN_NOW``."""
    return format_datetime(FROZEN_NOW + timedelta(seconds=offset_seconds), usegmt=True)


def _zoneless_date(offset_seconds: int) -> str:
    """Return the RFC 5322 date *offset_seconds* after ``FROZEN_NOW`` with the ``-0000`` "no zone" marker."""
    return format_datetime((FROZEN_NOW + timedelta(seconds=offset_seconds)).replace(tzinfo=None))


def _offset_date(offset_seconds: int, *, hours: int) -> str:
    """Return the RFC 5322 date naming the instant *offset_seconds* after ``FROZEN_NOW`` in the zone UTC+*hours*."""
    moment = (FROZEN_NOW + timedelta(seconds=offset_seconds)).astimezone(timezone(timedelta(hours=hours)))
    return format_datetime(moment)


def _rfc850_date(offset_seconds: int) -> str:
    """Return the obsolete RFC 850 date *offset_seconds* after ``FROZEN_NOW``."""
    moment = FROZEN_NOW + timedelta(seconds=offset_seconds)
    day, month = _WEEKDAYS[moment.weekday()], _MONTHS[moment.month - 1]
    return f"{day}, {moment.day:02d}-{month}-{moment.year % 100:02d} {moment:%H:%M:%S} GMT"


def _asctime_date(offset_seconds: int) -> str:
    """Return the asctime date *offset_seconds* after ``FROZEN_NOW``."""
    moment = FROZEN_NOW + timedelta(seconds=offset_seconds)
    day, month = _WEEKDAYS[moment.weekday()][:3], _MONTHS[moment.month - 1]
    return f"{day} {month} {moment.day:2d} {moment:%H:%M:%S} {moment.year}"


#: Every status class the retry layer distinguishes: success, redirect, the 4xx it leaves alone, 429, and the 5xx
#: range (501 included — the policy retries it, and that is reproduced, not fixed).
STATUSES = (200, 204, 301, 304, 400, 401, 403, 404, 418, 428, 429, 430, 499, 500, 501, 502, 503, 504, 511, 599)

#: Exceptions by name: four httpx2 errors and three OSErrors (retried), and two that are neither (re-raised at once).
EXCEPTIONS: dict[str, Callable[[httpx2.Request], Exception]] = {
    "ConnectError": lambda request: httpx2.ConnectError("boom", request=request),
    "ReadTimeout": lambda request: httpx2.ReadTimeout("boom", request=request),
    "RemoteProtocolError": lambda request: httpx2.RemoteProtocolError("boom", request=request),
    "DecodingError": lambda request: httpx2.DecodingError("boom", request=request),
    "OSError": lambda _request: OSError("boom"),
    "ConnectionResetError": lambda _request: ConnectionResetError("boom"),
    "TimeoutError": lambda _request: TimeoutError("boom"),
    "ValueError": lambda _request: ValueError("boom"),
    "RuntimeError": lambda _request: RuntimeError("boom"),
}

#: Every ``Retry-After`` form the parser distinguishes.
RETRY_AFTER_FORMS = (
    "7",  # whole seconds, above the 5 s cap
    "2",  # whole seconds, under it
    "0",  # zero: not a positive delay, so the backoff applies
    "1.5",  # a fraction: neither whole seconds nor a date
    "-1",  # negative: likewise
    " 3 ",  # padded: stripped
    "soon",  # garbage
    "",  # present but empty
    _http_date(3),  # a future date under the cap
    _http_date(30),  # a future date over the cap
    _http_date(-30),  # a past date: parses to 0, so the backoff applies
    _zoneless_date(3),  # a zone-less date ("-0000"): read as UTC
    "Mon, 32 Foo 2026 99:99:99 GMT",  # date-shaped garbage
)

#: The parser's edges, appended after the first 875 cases. The library accepts a delay only as ASCII digits or an
#: HTTP-date, so every lenient reading (``int()``, ``float()``, a fixed zone) is a way to differ from it.
RETRY_AFTER_EDGE_FORMS = (
    "+3",  # ``int()`` and ``float()`` accept a sign; the library does not: garbage, so the backoff applies
    "3_0",  # ``int()`` accepts underscores (as 30); the library does not
    "1e3",  # ``float()`` accepts an exponent; the library does not
    "inf",  # ``float()`` accepts it (and would then be capped to 5 s); the library does not
    "   ",  # whitespace only: stripped to empty, so no header
    "\uff11\uff12",  # fullwidth "12": ``str.isdigit()`` is true, ``isascii()`` is not, so not whole seconds
    _offset_date(3, hours=1),  # a date in a zone ahead of UTC, 3 s from now: the offset is honoured
    _offset_date(3, hours=-5),  # ...and behind it
    _rfc850_date(3),  # the obsolete RFC 850 date, 3 s from now
    _asctime_date(3),  # the asctime date: zone-less, so read as UTC
)

#: Every single network outcome: each status, then each exception.
OUTCOMES = (*(Step(status=status) for status in STATUSES), *(Step(raises=name) for name in EXCEPTIONS))

#: The outcomes that are not a success or a redirect: every exception and every 4xx/5xx.
FAILURES = tuple(step for step in OUTCOMES if step.raises is not None or (step.status or 0) >= httpx2.codes.BAD_REQUEST)

#: A successful send, the step most scripts end on.
_OK = Step(status=200)

#: A case before it is seeded: ``(attempts, method, script)``.
type _Shape = tuple[int, str, tuple[Step, ...]]


def _decision_shapes() -> Iterator[_Shape]:
    """Yield one outcome, then a success: the retry decision for each outcome, method and budget."""
    for attempts in (1, 2, 3):
        for method in ("GET", "POST", "PATCH", "OPTIONS", "TRACE"):
            yield from ((attempts, method, (step, _OK)) for step in OUTCOMES)


def _exhaustion_shapes() -> Iterator[_Shape]:
    """Yield the same failure every time: exhaustion, for each budget (0 is clamped like 1)."""
    for attempts in (0, 1, 2, 3, 5):
        for method in ("GET", "POST", "TRACE"):
            yield from ((attempts, method, (step,)) for step in FAILURES)


def _retry_after_shapes(forms: tuple[str, ...]) -> Iterator[_Shape]:
    """Yield every form on a retried status twice (the second wait is on attempt 2), then once on a 404."""
    for status in (429, 500, 501, 503):
        for value in forms:
            step = Step(status=status, retry_after=value)
            yield (3, "GET", (step, step, _OK))
    # On a status that is not retried, the header must change nothing.
    yield from ((3, "GET", (Step(status=404, retry_after=value), _OK)) for value in forms)


def _method_boundary_shapes() -> Iterator[_Shape]:
    """Yield all seven retried methods and two that are not, on each kind of outcome."""
    scripts = (
        (Step(status=503), _OK),
        (Step(status=429, retry_after="2"), _OK),
        (Step(raises="ConnectError"), _OK),
        (_OK,),
    )
    for method in ("GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS", "TRACE", "CONNECT"):
        yield from ((3, method, script) for script in scripts)


def _mixed_shapes() -> Iterator[_Shape]:
    """Yield scripts whose steps differ.

    Each sleep must read its own response's headers (none after an exception), and a retried failure followed by a
    not-retried one stops there.
    """
    mixed = (
        (Step(status=429, retry_after="7"), Step(raises="ConnectError"), Step(raises="ConnectError"), _OK),
        (Step(raises="ConnectError"), Step(status=429, retry_after="2"), _OK),
        (Step(status=503, retry_after="7"), Step(status=503), Step(status=503, retry_after=" 3 "), _OK),
        (Step(status=503), Step(raises="ConnectError"), Step(status=503, retry_after="7"), _OK),
        (Step(raises="ConnectError"), Step(raises="ValueError")),
        (Step(status=503), Step(status=404)),
        (Step(status=429, retry_after=_http_date(-30)), Step(status=503, retry_after="7"), _OK),
        (Step(raises="ReadTimeout"), Step(status=503), Step(raises="OSError"), _OK),
    )
    for attempts in (3, 5):
        for method in ("GET", "POST"):
            yield from ((attempts, method, script) for script in mixed)


def _build_cases() -> tuple[Case, ...]:
    """Build the matrix, seeding each case with its index.

    The first 875 cases come first so they keep their seeds and their rows; the parser's edges, the method boundary
    and the mixed scripts were appended after them.
    """
    shapes = (
        *_decision_shapes(),
        *_exhaustion_shapes(),
        *_retry_after_shapes(RETRY_AFTER_FORMS),
        *_retry_after_shapes(RETRY_AFTER_EDGE_FORMS),
        *_method_boundary_shapes(),
        *_mixed_shapes(),
    )
    return tuple(Case(attempts, method, script, seed=index) for index, (attempts, method, script) in enumerate(shapes))


#: The matrix: 435 + 375 + 52 + 13 = 875 cases, then 50 + 36 + 32 = 118 more: 993.
CASES = _build_cases()


async def run_case(make: TransportFactory, case: Case) -> Outcome:
    """Drive one case through the retry transport *make* builds, and report what its caller observes."""
    responses: list[httpx2.Response] = []
    sends = 0

    def network(request: httpx2.Request) -> httpx2.Response:
        nonlocal sends
        step = case.script[min(sends, len(case.script) - 1)]
        sends += 1
        if step.raises is not None:
            raise EXCEPTIONS[step.raises](request)
        # Bytes, not str: httpx2 encodes a str header as ASCII and raises on a non-ASCII one, so the parser's
        # ``isascii()`` branch could not be reached. Bytes arrive decoded as UTF-8 (verified against every form).
        headers: dict[bytes, bytes] = {} if step.retry_after is None else {b"Retry-After": step.retry_after.encode()}
        response = httpx2.Response(step.status or 200, headers=headers, stream=_UnreadStream())
        responses.append(response)
        return response

    sleeps: list[float] = []
    closed_at_sleep: list[tuple[bool, ...]] = []
    # The patch below is process-wide, so it reaches every thread's event loop: a coroutine sleeping on another
    # thread's loop during a case would otherwise be recorded into this case and, handed a sleep that
    # returns at once, spin for the whole case. Only this case's own task is the transport; any other really sleeps.
    case_task = asyncio.current_task()
    real_sleep = asyncio.sleep

    async def record_sleep(seconds: float, result: object = None) -> object:  # asyncio.sleep's own signature
        if asyncio.current_task() is not case_task:
            return await real_sleep(seconds, result)
        sleeps.append(seconds)
        closed_at_sleep.append(tuple(r.is_closed for r in responses))
        return result

    # The module-level generator, seeded per case, is the source the table was recorded against.
    random.seed(case.seed)
    transport = make(httpx2.MockTransport(network), case.attempts, random.random)
    request = httpx2.Request(case.method, "https://retry.test/x")
    with time_machine.travel(FROZEN_NOW, tick=False), patch.object(asyncio, "sleep", record_sleep):
        try:
            response = await transport.handle_async_request(request)
        except Exception as exc:  # the exception's class is the observed outcome, if the script raised it
            if type(exc).__name__ not in {step.raises for step in case.script}:
                raise  # a harness or transport bug, not a retry outcome: fail, do not write it into the table
            result, attempts_made = f"raise:{type(exc).__name__}", None
        else:
            result = f"status:{response.status_code}"
            context = CallContext.of(request)
            attempts_made = context.attempt - 1 if request.method in RETRIED_METHODS else None
    closed = tuple(r.is_closed for r in responses)
    return Outcome(sends, tuple(sleeps), result, attempts_made, closed, tuple(closed_at_sleep))


def case_json(case: Case) -> dict[str, object]:
    """Return *case* as the golden table stores it (tuples become lists)."""
    return msgspec.json.decode(json.dumps(asdict(case)), type=dict[str, object])


def outcome_json(outcome: Outcome) -> dict[str, object]:
    """Return *outcome* as the golden table stores it; floats round-trip exactly, as ``json`` writes their ``repr``."""
    return msgspec.json.decode(json.dumps(asdict(outcome)), type=dict[str, object])


def load_golden() -> list[dict[str, object]]:
    """Load the golden table: one ``{"case": ..., "outcome": ...}`` row per case, in matrix order."""
    return msgspec.json.decode(GOLDEN.read_bytes(), type=list[dict[str, object]])
