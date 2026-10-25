# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""``headgate.testing.faults``: each scheduled step does what it says, and the call log records it."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, override

import httpx2
import pytest

from headgate._core import CallContext, CallInfo
from headgate.retry._classify import refused_stream
from headgate.retry._tracking import head_size
from headgate.testing import (
    ConnectFail,
    DropMidBody,
    FaultyTransport,
    PoolFull,
    RefusedStream,
    Respond,
    Slow,
    TestClock,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from headgate.testing import Step

#: The body the tests send.
_BODY = b"0123456789"

#: A read timeout the tests stamp.
_READ_TIMEOUT = 5.0


def _request(*, timeout: dict[str, float | None] | None = None) -> tuple[httpx2.Request, CallContext]:
    """Build a POST with a ten-byte body and a fresh context whose first attempt has begun."""
    extensions = {} if timeout is None else {"timeout": timeout}
    request = httpx2.Request("POST", "https://api.test/orders", content=_BODY, extensions=extensions)
    context = CallContext(CallInfo(method="POST", url="https://api.test/orders"))
    context.attach(request)
    context.begin_attempt()
    return request, context


async def test_respond_answers_with_a_status_headers_and_a_json_body() -> None:
    """Answer with the step's status, headers and JSON body, and log what was sent."""
    network = FaultyTransport(schedule=[Respond(201, headers={"X-Id": "7"}, json={"ok": True}, retry_after=1.5)])
    request, context = _request()

    response = await network.handle_async_request(request)

    assert response.status_code == httpx2.codes.CREATED
    assert response.headers["X-Id"] == "7"
    assert response.headers["Retry-After"] == "1.5"
    assert json.loads(await response.aread()) == {"ok": True}
    call = network.calls[0]
    assert (call.method, call.url, call.connected) == ("POST", "https://api.test/orders", True)
    assert call.bytes_sent == head_size(request) + len(_BODY)
    assert context.progress.connected is True
    assert context.progress.bytes_sent == head_size(request), "the body is left for a counting layer to report"


async def test_a_response_records_when_it_is_closed() -> None:
    """Report an open response as unclosed, and closed once it is."""
    network = FaultyTransport(schedule=[Respond(200, content=b"x")])
    request, _ = _request()

    response = await network.handle_async_request(request)

    assert network.calls[0].response is response
    assert network.calls[0].response_closed is False
    await response.aclose()
    assert network.calls[0].response_closed is True


@pytest.mark.parametrize(
    ("step", "error"),
    [(ConnectFail(), httpx2.ConnectError), (ConnectFail(timeout=True), httpx2.ConnectTimeout)],
)
async def test_connect_fail_sends_nothing(step: ConnectFail, error: type[Exception]) -> None:
    """Raise the connect failure with nothing connected or written."""
    network = FaultyTransport(schedule=[step])
    request, context = _request()

    with pytest.raises(error):
        await network.handle_async_request(request)

    call = network.calls[0]
    assert (call.connected, call.bytes_sent, call.response_closed) == (False, 0, None)
    assert isinstance(call.error, error)
    assert (context.progress.connected, context.progress.bytes_sent) == (False, 0)


async def test_pool_full_sends_nothing() -> None:
    """Raise ``PoolTimeout`` with nothing connected or written."""
    network = FaultyTransport(schedule=[PoolFull()])
    request, _ = _request()

    with pytest.raises(httpx2.PoolTimeout):
        await network.handle_async_request(request)
    assert network.calls[0].connected is False


async def test_a_drop_before_anything_is_written_is_a_write_error_with_nothing_sent() -> None:
    """Connect, write nothing, and fail the write."""
    network = FaultyTransport(schedule=[DropMidBody(after_bytes=0)])
    request, context = _request()

    with pytest.raises(httpx2.WriteError):
        await network.handle_async_request(request)

    assert (network.calls[0].connected, network.calls[0].bytes_sent) == (True, 0)
    assert (context.progress.connected, context.progress.bytes_sent) == (True, 0)


async def test_a_drop_inside_the_body_writes_exactly_that_many_bytes() -> None:
    """Write the head and part of the body, then fail the write."""
    request, _ = _request()
    cut = head_size(request) + len(_BODY) // 2
    network = FaultyTransport(schedule=[DropMidBody(after_bytes=cut)])

    with pytest.raises(httpx2.WriteError):
        await network.handle_async_request(request)
    assert network.calls[0].bytes_sent == cut


async def test_a_drop_after_the_whole_request_is_a_disconnect_without_an_answer() -> None:
    """Write everything, then have the server hang up: ``RemoteProtocolError``."""
    network = FaultyTransport(schedule=[DropMidBody(after_bytes=10_000)])
    request, _ = _request()

    with pytest.raises(httpx2.RemoteProtocolError):
        await network.handle_async_request(request)
    assert network.calls[0].bytes_sent == head_size(request) + len(_BODY)


async def test_a_refused_stream_is_raised_as_httpx2_raises_it() -> None:
    """Write the head, then raise ``RemoteProtocolError`` chained from httpcore2's with the h2 event."""
    network = FaultyTransport(schedule=[RefusedStream()])
    request, _ = _request()

    with pytest.raises(httpx2.RemoteProtocolError) as raised:
        await network.handle_async_request(request)

    assert refused_stream(raised.value)
    assert network.calls[0].bytes_sent == head_size(request)


async def test_slow_waits_then_answers() -> None:
    """Wait the step's seconds through the injected sleep, then take its follow-up step."""
    clock = TestClock()
    network = FaultyTransport(schedule=[Slow(seconds=2.0, then=Respond(204))], sleep=clock.sleep)
    request, _ = _request(timeout={"read": _READ_TIMEOUT})

    response = await network.handle_async_request(request)

    assert response.status_code == httpx2.codes.NO_CONTENT
    assert clock.sleeps == [2.0]
    await response.aclose()


async def test_slow_past_the_read_timeout_times_out_at_the_timeout() -> None:
    """Wait only the read timeout, then raise ``ReadTimeout``."""
    clock = TestClock()
    network = FaultyTransport(schedule=[Slow(seconds=60.0)], sleep=clock.sleep)
    request, _ = _request(timeout={"read": _READ_TIMEOUT})

    with pytest.raises(httpx2.ReadTimeout):
        await network.handle_async_request(request)
    assert clock.sleeps == [_READ_TIMEOUT]


@pytest.mark.parametrize("timeouts", [None, {"read": None}])
async def test_slow_without_a_read_timeout_waits_in_full(timeouts: dict[str, float | None] | None) -> None:
    """Wait the whole delay when the request carries no read timeout."""
    clock = TestClock()
    network = FaultyTransport(schedule=[Slow(seconds=60.0)], sleep=clock.sleep)
    request, _ = _request(timeout=timeouts)

    await (await network.handle_async_request(request)).aclose()
    assert clock.sleeps == [60.0]


async def test_running_past_the_schedule_fails_the_test() -> None:
    """Raise ``AssertionError`` for a send the schedule did not plan for."""
    network = FaultyTransport(schedule=[])
    request, _ = _request()

    with pytest.raises(AssertionError, match="schedule exhausted"):
        await network.handle_async_request(request)


async def test_a_request_without_a_context_is_still_logged() -> None:
    """Log a bare request's send, with no call context to report progress to."""
    network = FaultyTransport(schedule=[Respond(200)])
    request = httpx2.Request("GET", "https://api.test/x")

    await (await network.handle_async_request(request)).aclose()

    assert network.calls[0].bytes_sent == head_size(request)
    assert CallContext.find(request) is None


class _SyncOnly(httpx2.SyncByteStream):
    """A body stream that can only be iterated synchronously."""

    @override
    def __iter__(self) -> Iterator[bytes]:
        yield b"x"


async def test_a_synchronous_body_stream_is_not_read() -> None:
    """Count only the head of a request whose body an async transport cannot read."""
    network = FaultyTransport(schedule=[Respond(200)])
    request = httpx2.Request("POST", "https://api.test/x", stream=_SyncOnly())

    await (await network.handle_async_request(request)).aclose()

    assert network.calls[0].bytes_sent == head_size(request)


# --- What each fault raises, exactly ---


@pytest.mark.parametrize(
    ("step", "error", "message"),
    [
        (ConnectFail(), httpx2.ConnectError, "scheduled connect failure"),
        (ConnectFail(timeout=True), httpx2.ConnectTimeout, "scheduled connect failure"),
        (PoolFull(), httpx2.PoolTimeout, "scheduled pool timeout"),
        (DropMidBody(after_bytes=3), httpx2.WriteError, "scheduled connection drop while writing"),
        (
            DropMidBody(after_bytes=10_000),
            httpx2.RemoteProtocolError,
            "Server disconnected without sending a response.",
        ),
        (Slow(seconds=_READ_TIMEOUT + 1), httpx2.ReadTimeout, "scheduled read timeout"),
    ],
    ids=["connect", "connect-timeout", "pool", "drop-writing", "drop-after", "read-timeout"],
)
async def test_each_fault_raises_its_httpx2_error_on_the_request(
    step: Step, error: type[httpx2.RequestError], message: str
) -> None:
    """Raise the httpx2 error each fault stands for, with its message and the request it failed."""
    network = FaultyTransport(schedule=[step], sleep=TestClock().sleep)
    request, _ = _request(timeout={"read": _READ_TIMEOUT})

    with pytest.raises(error) as raised:
        await network.handle_async_request(request)

    assert raised.type is error
    assert str(raised.value) == message
    assert raised.value.request is request


async def test_a_drop_after_exactly_the_head_is_a_disconnect_for_a_bodyless_request() -> None:
    """Count a request with no body as sent whole once its head is written."""
    request = httpx2.Request("GET", "https://api.test/orders")
    network = FaultyTransport(schedule=[DropMidBody(after_bytes=head_size(request))])

    with pytest.raises(httpx2.RemoteProtocolError):
        await network.handle_async_request(request)


async def test_a_refused_stream_names_stream_one_and_its_request() -> None:
    """Build the reset as httpcore2 reports it: the h2 event for stream 1, its text, and the request."""
    network = FaultyTransport(schedule=[RefusedStream()])
    request, _ = _request()

    with pytest.raises(httpx2.RemoteProtocolError) as raised:
        await network.handle_async_request(request)

    cause = raised.value.__cause__
    assert cause is not None
    assert cause.args[0].stream_id == 1
    assert str(raised.value) == str(cause)
    assert raised.value.request is request


async def test_slow_exactly_at_the_read_timeout_still_answers() -> None:
    """Answer a wait exactly as long as the read timeout: only a longer one times out."""
    clock = TestClock()
    network = FaultyTransport(schedule=[Slow(seconds=_READ_TIMEOUT)], sleep=clock.sleep)
    request, _ = _request(timeout={"read": _READ_TIMEOUT})

    response = await network.handle_async_request(request)

    assert response.status_code == httpx2.codes.OK
    assert response.request is request
    await response.aclose()


def test_respond_declares_its_json_and_its_retry_after() -> None:
    """Add ``Content-Type: application/json`` for a JSON body and ``Retry-After`` for a delay, after the step's own."""
    headers, body = Respond(json={"a": 1}, headers={"X-Id": "7"}, retry_after=2.0).response()

    assert headers == [("X-Id", "7"), ("Retry-After", "2"), ("Content-Type", "application/json")]
    assert body == b'{"a": 1}'


async def test_a_response_is_built_on_the_request_it_answers() -> None:
    """Build a plain step's response on the request it answers."""
    network = FaultyTransport(schedule=[Respond(204)])
    request, _ = _request()

    response = await network.handle_async_request(request)

    assert response.request is request
    await response.aclose()


async def test_a_drop_inside_the_head_of_a_bodyless_request_is_a_write_error() -> None:
    """Fail a request with no body whose head was cut short as a write, not as a disconnect after it."""
    network = FaultyTransport(schedule=[DropMidBody(after_bytes=3)])

    with pytest.raises(httpx2.WriteError):
        await network.handle_async_request(httpx2.Request("GET", "https://api.test/orders"))
