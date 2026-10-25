# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""The events a client emits to its observers: one ``CallStarted``, an ``AttemptFinished`` per send, ``CallFinished``.

Every event names its call with the same ``CallInfo``: the method, the URL as error messages name it (credentials
redacted), the t-string route template, and a call id. Durations are on the clock the client was built with.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx2
import pytest

from headgate import HttpOptions, QueryParamAuth, Rate, RetryPolicy, TransportPolicy, as_bytes, nothing
from headgate._core import Timing, timing_override
from headgate.errors import TransportError, UpstreamStatusError
from headgate.events import AttemptFinished, CallFinished, CallInfo, CallStarted, Event
from headgate.health import observer_failures
from headgate.retry import NO_RETRY
from tests.support.clients import StubClient
from tests.support.clock import FakeClock
from tests.support.wire import Script

if TYPE_CHECKING:
    from collections.abc import Callable

_BASE = "https://api.example.test/v1"
_RATE = Rate.per_second(1000)
#: How long each send takes on the test clock.
_SEND_TIME = 0.25


class _Recorder:
    """An observer that keeps every event it receives."""

    def __init__(self) -> None:
        """Start empty."""
        self.events: list[Event] = []

    def notify(self, event: Event) -> None:
        """Keep *event*."""
        self.events.append(event)


class _Broken:
    """An observer that raises on every event."""

    def notify(self, event: Event) -> None:
        """Raise."""
        raise RuntimeError(type(event).__name__)


def _client(
    handler: Callable[[httpx2.Request], httpx2.Response],
    *observers: _Recorder | _Broken,
    policy: TransportPolicy | None = None,
    auth: httpx2.Auth | None = None,
) -> StubClient:
    """Build a client over *handler* reporting to *observers*."""
    return StubClient(
        base_url=_BASE,
        rate=_RATE,
        auth=auth,
        policy=policy or TransportPolicy(),
        http=HttpOptions(transport=httpx2.MockTransport(handler), observers=observers),
    )


async def test_a_call_reports_its_start_each_send_and_its_end_under_one_call_info() -> None:
    """Emit started, finished-attempt, finished, all naming the call's sanitized URL and route template."""
    recorder = _Recorder()
    series_id = "GDP/real"
    client = _client(Script(), recorder, auth=QueryParamAuth({"api_key": "secret"}))
    await client.get(t"series/{series_id}", expect=nothing, params={"token": "t0k"})

    started, attempt, finished = recorder.events
    assert isinstance(started, CallStarted)
    assert isinstance(attempt, AttemptFinished)
    assert isinstance(finished, CallFinished)
    info = started.call
    assert info == CallInfo(method="GET", url="series/GDP%2Freal", route="series/{series_id}", call_id=info.call_id)
    assert attempt == AttemptFinished(call=info, attempt=1, status_code=200, elapsed=attempt.elapsed)
    assert finished == CallFinished(call=info, status_code=200, attempts=1, elapsed=finished.elapsed)
    assert "secret" not in repr(recorder.events)
    assert "t0k" not in repr(recorder.events)


async def test_a_str_url_has_no_route() -> None:
    """Report no route template for a ``str`` URL, which has none."""
    recorder = _Recorder()
    await _client(Script(), recorder).get("series", expect=nothing)
    started = recorder.events[0]
    assert isinstance(started, CallStarted)
    assert started.call.route is None


async def test_each_retry_is_its_own_attempt() -> None:
    """Report every send as an attempt, numbered from one, and the call's total."""
    recorder = _Recorder()
    await _client(Script(503, 503, 200), recorder).get("x", expect=nothing)

    attempts = [event for event in recorder.events if isinstance(event, AttemptFinished)]
    assert [(event.attempt, event.status_code) for event in attempts] == [(1, 503), (2, 503), (3, 200)]
    finished = recorder.events[-1]
    assert isinstance(finished, CallFinished)
    assert finished.attempts == len(attempts)


def _refuse(request: httpx2.Request) -> httpx2.Response:
    """Fail the connection."""
    msg = "connection refused"
    raise httpx2.ConnectError(msg, request=request)


async def test_a_failed_call_reports_the_error_types() -> None:
    """Name what a send raised on its attempt, and the ``ClientError`` the call raised on its end."""
    recorder = _Recorder()
    with pytest.raises(TransportError):
        await _client(_refuse, recorder, policy=TransportPolicy(retry=NO_RETRY)).get("x", expect=nothing)

    _, attempt, finished = recorder.events
    assert isinstance(attempt, AttemptFinished)
    assert (attempt.status_code, attempt.error) == (None, "ConnectError")
    assert isinstance(finished, CallFinished)
    assert (finished.status_code, finished.error) == (None, "TransportError")


async def test_an_undeclared_status_reports_both_the_status_and_the_error() -> None:
    """Report the final status and the error the status ladder raised for it."""
    recorder = _Recorder()
    with pytest.raises(UpstreamStatusError):
        await _client(Script(404), recorder).get("x", expect=nothing)

    finished = recorder.events[-1]
    assert isinstance(finished, CallFinished)
    assert (finished.status_code, finished.error) == (404, "UpstreamStatusError")


async def test_durations_are_measured_on_the_clients_clock() -> None:
    """Measure each send and the whole call on the clock the client was built with."""
    clock = FakeClock()

    def slow(request: httpx2.Request) -> httpx2.Response:
        clock.advance(_SEND_TIME)
        return httpx2.Response(200, request=request)

    recorder = _Recorder()
    with timing_override(Timing(clock=clock, sleep=clock.sleep)):
        client = _client(slow, recorder, policy=TransportPolicy(retry=RetryPolicy(attempts=1)))
    await client.get("x", expect=nothing)

    _, attempt, finished = recorder.events
    assert isinstance(attempt, AttemptFinished)
    assert isinstance(finished, CallFinished)
    assert attempt.elapsed == _SEND_TIME
    assert finished.elapsed == _SEND_TIME


async def test_a_failing_observer_never_breaks_the_call() -> None:
    """Complete the call, deliver to the other observers, and count the failures in ``headgate.health``."""
    before = observer_failures().get(_Broken.__qualname__, 0)
    recorder = _Recorder()
    body = await _client(Script(), _Broken(), recorder).get("x", expect=as_bytes)

    assert body == b""
    assert len(recorder.events) == len(("started", "attempt", "finished"))
    assert observer_failures()[_Broken.__qualname__] == before + len(recorder.events)


async def test_a_streamed_get_reports_its_start_and_end() -> None:
    """Report a ``stream_get`` call like any other, its status included."""
    recorder = _Recorder()
    chunks = [chunk async for chunk in _client(Script(httpx2.Response(200, content=b"abc")), recorder).stream_get("f")]

    assert b"".join(chunks) == b"abc"
    finished = recorder.events[-1]
    assert isinstance(finished, CallFinished)
    assert (finished.status_code, finished.error) == (200, None)


async def test_a_client_without_observers_emits_nothing_and_still_works() -> None:
    """Send normally with no observers installed."""
    script = Script()
    await StubClient(base_url=_BASE, rate=_RATE, http=HttpOptions(transport=script.transport())).get(
        "x", expect=nothing
    )
    assert len(script.sent) == 1
