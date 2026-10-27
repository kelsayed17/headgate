# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""``AsyncBaseClient.stream``: a response read as it arrives, through the same stack and failure ladder as a verb."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, assert_type, override

import httpx2
import pytest

from headgate import BreakerPolicy, HttpOptions, TransportPolicy
from headgate._body import DEFAULT_MAX_RESPONSE_BYTES
from headgate._responses import RefusedError
from headgate.errors import (
    ClientError,
    ClientTimeoutError,
    ResponseParseError,
    ResponseTooLargeError,
    TransportError,
    UpstreamStatusError,
)
from headgate.ratelimit import Rate
from headgate.streaming import Stream
from tests.support.breakers import failures
from tests.support.clients import StubClient

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Coroutine

    from headgate import RefusalCheck

_HOST = "x.test"
_BASE = f"https://{_HOST}"
_RATE = Rate.per_second(100)

#: A cap far below the streamed body, and a body far past it.
_SMALL_CAP = 100
_BIG_BODY = 5000

#: An error message is a bounded snippet plus a short prefix, whatever the size of the error body.
_MESSAGE_BOUND = 1000

#: A deadline short enough to pass while a test's handler waits, and a wait well past it.
_SHORT_DEADLINE = 0.05
_LONG_WAIT = 0.3

_SSE = {"content-type": "text/event-stream"}

type Handler = Callable[[httpx2.Request], Coroutine[None, None, httpx2.Response]]


def _client(
    handler: Handler,
    *,
    max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
    refusal_check: RefusalCheck | None = None,
) -> StubClient:
    """Build a client over an async in-memory *handler*; a *refusal_check* turns redirect-following off."""
    http = HttpOptions(
        transport=httpx2.MockTransport(handler),
        max_response_bytes=max_response_bytes,
        refusal_check=refusal_check,
        follow_redirects=refusal_check is None,
    )
    return StubClient(base_url=_BASE, rate=_RATE, http=http)


async def _drain(client: StubClient, on_chunk: Callable[[bytes], object] = len) -> None:
    """Stream ``f`` from *client* to its end, handing each chunk to *on_chunk* as it arrives."""
    async with client.stream("f") as stream:
        async for chunk in stream.aiter_bytes():
            on_chunk(chunk)


async def _drain_events(client: StubClient, max_event_size: int | None) -> None:
    """Read every event of ``events`` from *client*, with events up to *max_event_size* bytes."""
    async with client.stream("events") as stream:
        async for _ in stream.aiter_events(max_event_size=max_event_size):
            pass


def _answer(response: httpx2.Response) -> Handler:
    """Return a handler answering every request with a copy of *response*."""

    async def handle(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            response.status_code, headers=response.headers, content=response.content, request=request
        )

    return handle


class _Body(httpx2.AsyncByteStream):
    """A response body produced chunk by chunk, optionally failing after its chunks, recording whether it was closed."""

    def __init__(self, *chunks: bytes, then: Exception | None = None, gate: asyncio.Event | None = None) -> None:
        """Yield *chunks*; before the last, wait for *gate*; after them, raise *then*."""
        self._chunks = chunks
        self._then = then
        self._gate = gate
        self.closed = False

    @override
    async def __aiter__(self) -> AsyncIterator[bytes]:
        for index, chunk in enumerate(self._chunks):
            if self._gate is not None and index == len(self._chunks) - 1:
                await self._gate.wait()
            yield chunk
        if self._then is not None:
            raise self._then

    @override
    async def aclose(self) -> None:
        self.closed = True


def _streaming(body: _Body, headers: dict[str, str] | None = None) -> Handler:
    """Return a handler answering 200 with *body* streamed."""

    async def handle(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, headers=headers, stream=body, request=request)

    return handle


async def test_a_body_larger_than_the_response_cap_streams_through() -> None:
    """Check that a stream is not held to the buffered-body cap."""
    client = _client(_answer(httpx2.Response(200, content=b"x" * _BIG_BODY)), max_response_bytes=_SMALL_CAP)
    async with client.stream("big.csv") as stream:
        body = b"".join([chunk async for chunk in stream.aiter_bytes()])
    assert len(body) == _BIG_BODY


async def test_the_head_is_exposed_with_the_request_id() -> None:
    """Expose the status, the headers and the upstream's request id."""
    headers = {"x-request-id": "req-1", "x-extra": "yes"}
    async with _client(_answer(httpx2.Response(201, headers=headers))).stream("jobs", method="POST") as stream:
        assert (stream.status_code, stream.headers["x-extra"], stream.request_id) == (201, "yes", "req-1")
        assert stream.last_event_id is None


async def test_lines_are_iterated_without_their_endings() -> None:
    """Iterate the body as text lines."""
    async with _client(_answer(httpx2.Response(200, content=b"a,b\r\nc,d\n"))).stream("f.csv") as stream:
        assert [line async for line in stream.aiter_lines()] == ["a,b", "c,d"]


async def test_a_large_error_body_is_read_only_to_a_snippet() -> None:
    """Raise a non-2xx on entering the block, reading only a bounded head of its error body."""
    client = _client(_answer(httpx2.Response(400, content=b"bad " * 50_000)))
    with pytest.raises(UpstreamStatusError) as raised:
        async with client.stream("big.csv"):
            pytest.fail("the block runs only for a 2xx")
    assert raised.value.status_code == httpx2.codes.BAD_REQUEST
    assert len(str(raised.value)) < _MESSAGE_BOUND


async def test_a_redirect_that_is_not_followed_raises() -> None:
    """Raise a 3xx the call does not follow, since only a 2xx opens a stream."""
    client = _client(_answer(httpx2.Response(302, headers={"location": "/elsewhere"})))
    with pytest.raises(UpstreamStatusError) as raised:
        async with client.stream("f", follow_redirects=False):
            pytest.fail("the block runs only for a 2xx")
    assert raised.value.status_code == httpx2.codes.FOUND


async def test_a_refusal_check_runs_on_the_head_before_the_block() -> None:
    """Refuse a stream through the host check before the caller's block runs."""
    client = _client(
        _answer(httpx2.Response(200, content=b"data")),
        refusal_check=lambda resp: RefusedError("no") if resp.status_code == httpx2.codes.OK else None,
    )
    with pytest.raises(UpstreamStatusError, match="no"):
        async with client.stream("f"):
            pytest.fail("a refused stream never opens")
    with pytest.raises(ValueError, match="refusal"):
        async with client.stream("f", follow_redirects=True):
            pytest.fail("a refusing client never follows redirects")


async def test_a_5xx_that_outlasts_retries_raises_and_a_retried_one_opens() -> None:
    """Raise a 503 surviving every retry; open the stream when a retry succeeds."""
    sent: list[int] = []

    async def flaky(request: httpx2.Request) -> httpx2.Response:
        sent.append(1)
        status = 503 if len(sent) == 1 else 200
        return httpx2.Response(status, content=b"ok", request=request)

    async with _client(flaky).stream("f") as stream:
        assert [chunk async for chunk in stream.aiter_bytes()] == [b"ok"]
    assert len(sent) == len(("503", "200"))
    with pytest.raises(UpstreamStatusError) as raised:
        async with _client(_answer(httpx2.Response(503))).stream("f"):
            pytest.fail("the block runs only for a 2xx")
    assert raised.value.status_code == httpx2.codes.SERVICE_UNAVAILABLE


async def test_a_second_stream_works_after_a_failed_one() -> None:
    """Release a failed stream's connection, so the next one runs."""
    statuses = iter((404, 200))

    async def handle(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(next(statuses), content=b"ok", request=request)

    client = _client(handle)
    with pytest.raises(UpstreamStatusError):
        async with client.stream("a"):
            pytest.fail("the block runs only for a 2xx")
    async with client.stream("b") as stream:
        assert await anext(stream.aiter_bytes()) == b"ok"


async def test_the_response_is_closed_when_the_block_exits_unread() -> None:
    """Close the response on leaving the block, even when the body was not read to its end."""
    body = _Body(b"one", b"two")
    async with _client(_streaming(body)).stream("f") as stream:
        assert await anext(stream.aiter_bytes()) == b"one"
        assert not body.closed
    assert body.closed
    with pytest.raises(httpx2.StreamError):
        await anext(stream.aiter_bytes())


async def test_a_network_failure_mid_body_raises_from_the_iteration_and_counts_against_the_host() -> None:
    """Raise ``TransportError`` from the iteration, after the earlier chunks, and charge the host's breaker."""
    body = _Body(b"head", then=httpx2.ReadError("connection reset"))
    received: list[bytes] = []
    with pytest.raises(TransportError) as raised:
        await _drain(_client(_streaming(body)), received.append)
    assert received == [b"head"]
    assert not isinstance(raised.value, ClientTimeoutError)
    assert isinstance(raised.value.__cause__, httpx2.ReadError)
    assert failures(_HOST) == 1


async def test_a_timeout_mid_body_raises_a_client_timeout() -> None:
    """Map a read timeout while the body arrives onto ``ClientTimeoutError``."""
    body = _Body(b"head", then=httpx2.ReadTimeout("slow"))
    with pytest.raises(ClientTimeoutError):
        await _drain(_client(_streaming(body)))


async def test_an_error_of_the_callers_own_passes_through_and_charges_nothing() -> None:
    """Let an exception the caller raises in the block propagate unchanged, charging the host nothing."""

    class _MineError(Exception):
        """The caller's own failure."""

    with pytest.raises(_MineError):
        async with _client(_answer(httpx2.Response(200))).stream("f"):
            raise _MineError
    assert failures(_HOST) == 0


async def test_the_deadline_bounds_opening_the_stream() -> None:
    """Raise ``ClientTimeoutError`` when the response head does not arrive within the deadline."""

    async def slow(request: httpx2.Request) -> httpx2.Response:
        await asyncio.sleep(_LONG_WAIT)
        return httpx2.Response(200, request=request)

    with pytest.raises(ClientTimeoutError, match="deadline exceeded"):
        async with _client(slow).stream("f", deadline=_SHORT_DEADLINE):
            pytest.fail("the stream never opens")


async def test_the_deadline_does_not_bound_reading_the_body() -> None:
    """Read a body for longer than the deadline once the stream is open."""
    gate = asyncio.Event()
    body = _Body(b"a", b"b", gate=gate)
    async with _client(_streaming(body)).stream("f", deadline=_SHORT_DEADLINE) as stream:
        chunks = stream.aiter_bytes()
        assert await anext(chunks) == b"a"
        await asyncio.sleep(_SHORT_DEADLINE * 2)
        gate.set()
        assert await anext(chunks) == b"b"


async def test_events_are_parsed_as_they_arrive() -> None:
    """Yield each event as soon as its blank line arrives, with its event name, data, id and retry."""
    gate = asyncio.Event()
    body = _Body(b"event: tick\nid: 7\nretry: 1500\ndata: one\ndata: two\n\n", b"data: three\n\n", gate=gate)
    async with _client(_streaming(body, _SSE)).stream("events") as stream:
        events = stream.aiter_events()
        first = await anext(events)
        assert (first.event, first.data, first.id, first.retry) == ("tick", "one\ntwo", "7", 1500)
        assert stream.last_event_id == "7"
        gate.set()
        second = await anext(events)
        assert (second.event, second.data, second.id) == ("message", "three", "7")
        assert [event async for event in events] == []


async def test_a_stream_resumes_after_the_last_event_id() -> None:
    """Send ``Last-Event-ID`` when re-opening with the id the previous stream saw, and none for an empty one."""
    seen: list[str | None] = []

    async def handle(request: httpx2.Request) -> httpx2.Response:
        seen.append(request.headers.get("last-event-id"))
        return httpx2.Response(200, headers=_SSE, content=b"id: 41\ndata: x\n\n", request=request)

    client = _client(handle)
    async with client.stream("events", headers={"x-trace": "1"}) as stream:
        last = [event.id async for event in stream.aiter_events()][-1]
    async with client.stream("events", last_event_id=last):
        pass
    async with client.stream("events", last_event_id=""):
        pass
    assert seen == [None, "41", None]


async def test_events_require_an_event_stream() -> None:
    """Refuse to read events from a response that does not declare ``text/event-stream``."""
    async with _client(_answer(httpx2.Response(200, json={"a": 1}))).stream("events") as stream:
        with pytest.raises(ResponseParseError, match="application/json") as raised:
            stream.aiter_events()
    assert raised.value.status_code == httpx2.codes.OK
    async with _client(_answer(httpx2.Response(200))).stream("events") as stream:
        with pytest.raises(ResponseParseError, match="none"):
            stream.aiter_events()


async def test_an_oversized_event_is_too_large_and_charges_nothing() -> None:
    """Raise ``ResponseTooLargeError`` for an event past the size limit, charging the host nothing."""
    body = _Body(b"data: " + b"x" * _BIG_BODY + b"\n\n")
    with pytest.raises(ResponseTooLargeError, match="server-sent event"):
        await _drain_events(_client(_streaming(body, _SSE)), _SMALL_CAP)
    assert failures(_HOST) == 0


async def test_a_network_failure_mid_events_is_a_transport_error() -> None:
    """Map a network failure while events arrive onto ``TransportError``."""
    body = _Body(b"data: a\n\n", then=httpx2.RemoteProtocolError("peer closed"))
    with pytest.raises(TransportError):
        await _drain_events(_client(_streaming(body, _SSE)), None)
    assert failures(_HOST) == 1


async def test_the_block_receives_a_stream() -> None:
    """Hand the block a :class:`~headgate.streaming.Stream`, its status already known."""
    async with _client(_answer(httpx2.Response(200))).stream("f") as stream:
        assert_type(stream, Stream)
        assert stream.status_code == httpx2.codes.OK


async def test_a_stream_honors_the_policy_breaker() -> None:
    """Refuse to open a stream to a host whose breaker is open."""
    client = StubClient(
        base_url=_BASE,
        rate=_RATE,
        policy=TransportPolicy(),
        http=HttpOptions(transport=httpx2.MockTransport(_answer(httpx2.Response(503)))),
    )
    for _ in range(BreakerPolicy().failure_threshold):
        with pytest.raises(UpstreamStatusError):
            async with client.stream("f"):
                pass
    with pytest.raises(ClientError, match="circuit breaker open"):
        async with client.stream("f"):
            pass


# --- Exact shapes ---

#: The chunk size a caller asks for.
_CHUNK = 4


async def test_bytes_come_in_the_chunk_size_asked_for() -> None:
    """Re-chunk the body to *chunk_size* bytes when the caller asks for it."""
    client = _client(_answer(httpx2.Response(200, content=b"0123456789")))
    async with client.stream("f") as stream:
        chunks = [chunk async for chunk in stream.aiter_bytes(_CHUNK)]
    assert chunks == [b"0123", b"4567", b"89"]


async def test_a_connection_dropping_mid_lines_is_a_transport_error() -> None:
    """Map a network failure while lines arrive to ``TransportError``, as for bytes."""
    body = _Body(b"first\n", then=httpx2.ReadError("connection dropped"))
    with pytest.raises(TransportError):
        async with _client(_streaming(body)).stream("f") as stream:
            _ = [line async for line in stream.aiter_lines()]


async def test_an_event_stream_with_parameters_is_an_event_stream() -> None:
    """Read events from a ``text/event-stream`` that names a charset."""
    handler = _answer(
        httpx2.Response(200, headers={"content-type": "text/event-stream; charset=utf-8"}, content=b"data: a\n\n")
    )
    async with _client(handler).stream("events") as stream:
        assert [event.data async for event in stream.aiter_events()] == ["a"]


async def test_a_stream_without_a_content_type_is_named_as_such() -> None:
    """Say ``'none'`` when a stream read as events declares no content type."""
    async with _client(_answer(httpx2.Response(200, content=b"data: a\n\n"))).stream("events") as stream:
        with pytest.raises(ResponseParseError, match=r"got content type 'none'$"):
            stream.aiter_events()


async def test_an_event_past_the_size_limit_names_the_limit_and_the_call() -> None:
    """Refuse an event past ``max_event_size``, naming the limit and the call."""
    headers = {**_SSE, "x-request-id": "req-7"}
    handler = _answer(httpx2.Response(200, headers=headers, content=b"data: " + b"x" * 64 + b"\n\n"))
    with pytest.raises(ResponseTooLargeError) as raised:
        await _drain_events(_client(handler), max_event_size=16)

    error = raised.value
    assert str(error) == "[StubClient] server-sent event over 16 bytes from GET events"
    assert (error.method, error.url, error.request_id) == ("GET", "events", "req-7")
