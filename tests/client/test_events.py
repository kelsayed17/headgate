# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""The events a client emits to its observers: one ``CallStarted``, an ``AttemptFinished`` per send, ``CallFinished``.

Every event names its call with the same ``CallInfo``: the method, the URL as error messages name it (credentials
redacted), the t-string route template, and a call id. Durations are on the clock the client was built with.
"""

from __future__ import annotations

import httpx2
import pytest

from headgate import HttpOptions, QueryParamAuth, Rate, RetryPolicy, TransportPolicy, as_bytes, nothing
from headgate.errors import TransportError, UpstreamStatusError
from headgate.events import AttemptFinished, CallFinished, CallInfo, CallStarted, Event
from headgate.health import observer_failures
from headgate.retry import NO_RETRY
from headgate.testing import FakeTransport, Reply, TestClock
from tests.support.clients import StubClient
from tests.support.events import Recorder

_BASE = "https://api.example.test/v1"
_RATE = Rate.per_second(1000)
#: How long each send takes on the test clock.
_SEND_TIME = 0.25


class _Broken:
    """An observer that raises on every event."""

    def notify(self, event: Event) -> None:
        """Raise."""
        raise RuntimeError(type(event).__name__)


def _get(path: str, *replies: Reply) -> FakeTransport:
    """Return a fake network answering ``GET /v1/<path>`` with *replies*."""
    return FakeTransport().reply("GET", f"/v1/{path}", *replies)


def _client(
    fake: FakeTransport,
    *observers: Recorder | _Broken,
    policy: TransportPolicy | None = None,
    auth: httpx2.Auth | None = None,
) -> StubClient:
    """Build a client over *fake* reporting to *observers*."""
    return StubClient(
        base_url=_BASE,
        rate=_RATE,
        auth=auth,
        policy=policy or TransportPolicy(),
        http=HttpOptions(transport=fake, observers=observers),
    )


async def test_a_call_reports_its_start_each_send_and_its_end_under_one_call_info() -> None:
    """Emit started, finished-attempt, finished, all naming the call's sanitized URL and route template."""
    recorder = Recorder()
    series_id = "GDP/real"
    client = _client(_get("series/GDP%2Freal"), recorder, auth=QueryParamAuth({"api_key": "secret"}))
    await client.get(t"series/{series_id}", expect=nothing, params={"token": "t0k"})

    started, attempt, finished = recorder.events
    assert isinstance(started, CallStarted)
    assert isinstance(attempt, AttemptFinished)
    assert isinstance(finished, CallFinished)
    info = started.call
    assert info == CallInfo(
        method="GET",
        url="series/GDP%2Freal",
        route="series/{series_id}",
        full_url=f"{_BASE}/series/GDP%2Freal?token=%2A%2A%2A",
        call_id=info.call_id,
    )
    assert attempt == AttemptFinished(call=info, attempt=1, status_code=200, elapsed=attempt.elapsed)
    assert finished == CallFinished(call=info, status_code=200, attempts=1, elapsed=finished.elapsed)
    assert "secret" not in repr(recorder.events)
    assert "t0k" not in repr(recorder.events)


async def test_a_str_url_has_no_route() -> None:
    """Report no route template for a ``str`` URL, which has none."""
    recorder = Recorder()
    await _client(_get("series"), recorder).get("series", expect=nothing)
    started = recorder.events[0]
    assert isinstance(started, CallStarted)
    assert started.call.route is None


async def test_each_retry_is_its_own_attempt() -> None:
    """Report every send as an attempt, numbered from one, and the call's total."""
    recorder = Recorder()
    await _client(_get("x", 503, 503, 200), recorder).get("x", expect=nothing)

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
    recorder = Recorder()
    with pytest.raises(TransportError):
        await _client(_get("x", _refuse), recorder, policy=TransportPolicy(retry=NO_RETRY)).get("x", expect=nothing)

    _, attempt, finished = recorder.events
    assert isinstance(attempt, AttemptFinished)
    assert (attempt.status_code, attempt.error) == (None, "ConnectError")
    assert isinstance(finished, CallFinished)
    assert (finished.status_code, finished.error) == (None, "TransportError")


async def test_an_undeclared_status_reports_both_the_status_and_the_error() -> None:
    """Report the final status and the error the status ladder raised for it."""
    recorder = Recorder()
    with pytest.raises(UpstreamStatusError):
        await _client(_get("x", 404), recorder).get("x", expect=nothing)

    finished = recorder.events[-1]
    assert isinstance(finished, CallFinished)
    assert (finished.status_code, finished.error) == (404, "UpstreamStatusError")


async def test_durations_are_measured_on_the_clients_clock() -> None:
    """Measure each send and the whole call on the clock the client was built with."""
    clock = TestClock()

    def slow(request: httpx2.Request) -> httpx2.Response:
        clock.advance(_SEND_TIME)
        return httpx2.Response(200, request=request)

    recorder = Recorder()
    with clock.install():
        client = _client(_get("x", slow), recorder, policy=TransportPolicy(retry=RetryPolicy(attempts=1)))
    await client.get("x", expect=nothing)

    _, attempt, finished = recorder.events
    assert isinstance(attempt, AttemptFinished)
    assert isinstance(finished, CallFinished)
    assert attempt.elapsed == _SEND_TIME
    assert finished.elapsed == _SEND_TIME


async def test_a_failing_observer_never_breaks_the_call() -> None:
    """Complete the call, deliver to the other observers, and count the failures in ``headgate.health``."""
    before = observer_failures().get(_Broken.__qualname__, 0)
    recorder = Recorder()
    body = await _client(_get("x"), _Broken(), recorder).get("x", expect=as_bytes)

    assert body == b""
    assert len(recorder.events) == len(("started", "attempt", "finished"))
    assert observer_failures()[_Broken.__qualname__] == before + len(recorder.events)


async def test_a_streamed_get_reports_its_start_and_end() -> None:
    """Report a ``stream`` call like any other, its status included."""
    recorder = Recorder()
    client = _client(_get("f", httpx2.Response(200, content=b"abc")), recorder)
    async with client.stream("GET", "f") as stream:
        chunks = [chunk async for chunk in stream.aiter_bytes()]

    assert b"".join(chunks) == b"abc"
    finished = recorder.events[-1]
    assert isinstance(finished, CallFinished)
    assert (finished.status_code, finished.error) == (200, None)


async def test_a_client_without_observers_emits_nothing_and_still_works() -> None:
    """Send normally with no observers installed."""
    fake = _get("x")
    await StubClient(base_url=_BASE, rate=_RATE, http=HttpOptions(transport=fake)).get("x", expect=nothing)
    assert len(fake.calls) == 1
