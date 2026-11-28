# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""``headgate.testing``'s fault steps: each scheduled step does what it says, and the call log records it."""

from __future__ import annotations

import dataclasses
import json
from typing import TYPE_CHECKING, Any, cast, override

import httpx2
import pytest

from headgate._core import CallContext, CallInfo
from headgate._core._retry_after import parse_retry_after
from headgate.retry._classify import refused_stream
from headgate.retry._tracking import SendTrackingTransport, head_size
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
from headgate.testing._faults import response_parts
from tests.support.timing import built_on

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

    from headgate.testing import Step

#: The body the tests send.
_BODY = b"0123456789"

#: A read timeout the tests stamp.
_READ_TIMEOUT = 5.0


def _request(*, timeout: dict[str, float | None] | None = None) -> tuple[httpx2.Request, CallContext]:
    """Build a POST with a ten-byte body and a fresh context whose first attempt has begun."""
    extensions = {} if timeout is None else {"timeout": timeout}
    request = httpx2.Request("POST", "https://api.test/orders", content=_BODY, extensions=extensions)
    context = CallContext(CallInfo(method="POST", url="https://api.test/orders", full_url="https://api.test/orders"))
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


async def test_the_log_of_sends_is_read_only() -> None:
    """Log the sends as ``FakeTransport`` does: a fresh tuple to read, which no test can rewrite under the fake."""
    network = FaultyTransport(schedule=[Respond(204)])
    request, _ = _request()
    await network.handle_async_request(request)

    calls = network.calls

    assert type(calls) is tuple
    assert [call.method for call in calls] == ["POST"]
    with pytest.raises(AttributeError):
        cast("Any", network).calls = ()


async def test_a_snapshots_headers_are_its_own() -> None:
    """Copy the headers into each snapshot of a send, so a test that edits one changes neither the log nor another."""
    network = FaultyTransport(schedule=[Respond(204)])
    request, _ = _request()
    await network.handle_async_request(request)

    network.calls[0].headers["X-Edited"] = "1"

    assert "X-Edited" not in network.calls[0].headers


async def test_a_response_records_when_it_is_closed() -> None:
    """Report an open response as unclosed, and closed once it is."""
    network = FaultyTransport(schedule=[Respond(200, content=b"x")])
    request, _ = _request()

    response = await network.handle_async_request(request)

    assert network.calls[0].response is response
    before = network.calls[0]
    assert before.response_closed is False
    await response.aclose()
    assert network.calls[0].response_closed is True
    assert before.response_closed is False, "a FaultCall is the send as it stood when it was read"


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


async def _halves() -> AsyncIterator[bytes]:
    """Yield the test body in two chunks, as a streamed upload arrives."""
    yield _BODY[:5]
    yield _BODY[5:]


def _streamed() -> httpx2.Request:
    """Build a POST whose ten-byte body is streamed in two chunks."""
    return httpx2.Request("POST", "https://api.test/orders", content=_halves())


#: The size of :func:`_streamed`'s head.
_HEAD = head_size(_streamed())


@pytest.mark.parametrize(
    ("step", "written", "reported"),
    [
        pytest.param(DropMidBody(after_bytes=0), 0, 0, id="drop-before-the-head"),
        pytest.param(DropMidBody(after_bytes=3), 3, _HEAD, id="drop-inside-the-head"),
        pytest.param(DropMidBody(after_bytes=_HEAD + 4), _HEAD + 4, _HEAD + 5, id="drop-inside-a-chunk"),
        pytest.param(DropMidBody(after_bytes=10_000), _HEAD + 10, _HEAD + 10, id="drop-after-the-request"),
        pytest.param(RefusedStream(), _HEAD, _HEAD, id="refused-stream"),
    ],
)
async def test_progress_through_the_tracking_layer_is_what_httpx2_reports(
    step: Step, written: int, reported: int
) -> None:
    """Report what httpx2's own transport would: the whole head once it starts, and each body chunk the write took.

    The call log keeps the exact bytes written. A drop inside a chunk counts that chunk, which the transport took, and
    no chunk after it.
    """
    request = _streamed()
    context = CallContext(CallInfo(method="POST", url="https://api.test/orders", full_url="https://api.test/orders"))
    context.attach(request)
    context.begin_attempt()
    network = FaultyTransport(schedule=[step])

    with pytest.raises(httpx2.TransportError):
        await SendTrackingTransport(transport=network).handle_async_request(request)

    assert network.calls[0].bytes_sent == written
    assert (context.progress.connected, context.progress.bytes_sent) == (True, reported)


async def test_slow_waits_then_answers() -> None:
    """Wait the step's seconds through the injected sleep, then take its follow-up step."""
    clock = TestClock()
    network = built_on(clock.timing, lambda: FaultyTransport(schedule=[Slow(seconds=2.0, then=Respond(204))]))
    request, _ = _request(timeout={"read": _READ_TIMEOUT})

    response = await network.handle_async_request(request)

    assert response.status_code == httpx2.codes.NO_CONTENT
    assert clock.sleeps == [2.0]
    await response.aclose()


async def test_slow_past_the_read_timeout_times_out_at_the_timeout() -> None:
    """Wait only the read timeout, then raise ``ReadTimeout``."""
    clock = TestClock()
    network = built_on(clock.timing, lambda: FaultyTransport(schedule=[Slow(seconds=60.0)]))
    request, _ = _request(timeout={"read": _READ_TIMEOUT})

    with pytest.raises(httpx2.ReadTimeout):
        await network.handle_async_request(request)
    assert clock.sleeps == [_READ_TIMEOUT]


@pytest.mark.parametrize("timeouts", [None, {"read": None}])
async def test_slow_without_a_read_timeout_waits_in_full(timeouts: dict[str, float | None] | None) -> None:
    """Wait the whole delay when the request carries no read timeout."""
    clock = TestClock()
    network = built_on(clock.timing, lambda: FaultyTransport(schedule=[Slow(seconds=60.0)]))
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
    network = built_on(TestClock().timing, lambda: FaultyTransport(schedule=[step]))
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
    network = built_on(clock.timing, lambda: FaultyTransport(schedule=[Slow(seconds=_READ_TIMEOUT)]))
    request, _ = _request(timeout={"read": _READ_TIMEOUT})

    response = await network.handle_async_request(request)

    assert response.status_code == httpx2.codes.OK
    assert response.request is request
    await response.aclose()


def test_a_respond_step_is_read_by_its_fields() -> None:
    """Leave building the reply to the fakes: a ``Respond`` step shows what it was given."""
    assert not hasattr(Respond, "response")


def test_respond_declares_its_json_and_its_retry_after() -> None:
    """Add ``Content-Type: application/json`` for a JSON body and ``Retry-After`` for a delay, after the step's own."""
    headers, body = response_parts(Respond(json={"a": 1}, headers={"X-Id": "7"}, retry_after=2.0))

    assert headers == [("X-Id", "7"), ("Retry-After", "2"), ("Content-Type", "application/json")]
    assert body == b'{"a": 1}'


def test_respond_sends_json_under_the_content_type_its_headers_set() -> None:
    """Keep the ``Content-Type`` the step's headers set, in any case, for ``json=``, rather than add a second one."""
    step = Respond(404, json={"title": "Gone"}, headers={"content-type": "application/problem+json"})

    assert response_parts(step)[0] == [("content-type", "application/problem+json")]


def test_respond_takes_a_retry_after_one_way() -> None:
    """Refuse ``retry_after=`` beside a ``Retry-After`` the headers set, in any case, which sent the header twice."""
    with pytest.raises(ValueError, match=r"^Respond takes a Retry-After as retry_after= or in headers=, not both: "):
        Respond(503, headers={"retry-after": "5"}, retry_after=3)


def test_respond_takes_a_body_one_way() -> None:
    """Refuse ``content=`` beside ``json=``, where ``json=`` won unseen; a copy that changes either field is one too.

    So a ``Respond`` has no field of its own for the encoding, and ``dataclasses.replace`` gives another body or
    another status as it does any field.
    """
    with pytest.raises(ValueError, match=r"^Respond takes a body as json= or as content=, not both: json= is "):
        Respond(json={"a": 1}, content=b"other")
    step = Respond(json={"a": 1})

    assert (step.content, response_parts(step)[1]) == (b"", b'{"a": 1}')
    assert [field.name for field in dataclasses.fields(Respond)] == [
        "status_code",
        "headers",
        "json",
        "content",
        "retry_after",
    ]
    assert dataclasses.replace(step, status_code=503) == Respond(503, json={"a": 1})
    assert response_parts(dataclasses.replace(step, json={"b": 2}))[1] == b'{"b": 2}'


def test_respond_sends_each_line_of_a_repeated_header() -> None:
    """Send each ``Set-Cookie`` line of an ``httpx2.Headers``, which a response may repeat, rather than join them."""
    step = Respond(headers=httpx2.Headers([("set-cookie", "a=1"), ("set-cookie", "b=2")]))

    assert response_parts(step)[0] == [("set-cookie", "a=1"), ("set-cookie", "b=2")]


def test_respond_sends_its_json_as_json_dumps_spells_it() -> None:
    """Send ``json=`` as the step always sent it: ``json.dumps``'s defaults, ASCII, a space after each separator."""
    step = Respond(json={"name": "é", "tags": ["a"]})

    assert response_parts(step)[1] == b'{"name": "\\u00e9", "tags": ["a"]}'


@pytest.mark.parametrize(
    ("seconds", "header"),
    [(1e6, "1000000"), (1e22, "10000000000000000000000"), (0, "0"), (2.5, "2.5"), (1e-7, "0.0000001")],
    ids=["million", "huge", "zero", "fraction", "tiny-fraction"],
)
def test_a_retry_after_is_written_as_plain_decimal_seconds(seconds: float, header: str) -> None:
    """Write whole seconds as RFC 9110's delta-seconds and a fraction in plain digits, never with an exponent.

    Either way the value reads back as the same number of seconds, as headgate's own ``Retry-After`` parser reads it.
    """
    headers, _ = response_parts(Respond(503, retry_after=seconds))

    assert headers == [("Retry-After", header)]
    assert parse_retry_after(header, now=0.0) == seconds


@pytest.mark.parametrize("seconds", [-1.0, float("inf"), float("nan")], ids=["negative", "infinite", "nan"])
def test_a_retry_after_that_is_no_number_of_seconds_is_refused(seconds: float) -> None:
    """Refuse a negative or non-finite ``retry_after`` when the step is built: no ``Retry-After`` can say it."""
    with pytest.raises(ValueError, match="retry_after"):
        Respond(503, retry_after=seconds)


@pytest.mark.parametrize("seconds", [-1.0, float("nan")], ids=["negative", "nan"])
def test_a_slow_step_that_waits_no_number_of_seconds_is_refused(seconds: float) -> None:
    """Refuse a negative or NaN wait when the step is built, which no server takes; an infinite one never answers."""
    with pytest.raises(ValueError, match=rf"^seconds= takes seconds, a number of 0 or more \(got {seconds}\)$"):
        Slow(seconds=seconds)
    assert Slow(seconds=float("inf")).seconds == float("inf")


def test_a_drop_after_fewer_than_no_bytes_is_refused() -> None:
    """Refuse a negative byte count when the step is built: ``after_bytes=0`` is the drop before anything is written."""
    with pytest.raises(ValueError, match=r"^after_bytes= takes a number of bytes, an int of 0 or more \(got -1\)$"):
        DropMidBody(after_bytes=-1)


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
