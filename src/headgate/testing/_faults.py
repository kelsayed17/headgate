# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""Fault injection: a network transport that fails on a schedule, and records what every attempt sent.

:class:`FaultyTransport` stands in for the network (``HttpOptions(transport=FaultyTransport(...))``) and answers each
send with the next step of its schedule::

    FaultyTransport(schedule=[ConnectFail(), Respond(503, retry_after=1), DropMidBody(after_bytes=100),
                              Slow(seconds=2), RefusedStream(), Respond(200, json={"ok": True})])

It reports each send's progress the way httpx2's own transport does through its trace: a connection obtained marks
the call's current attempt connected, and the request head is counted as written when it starts to go out. The body
is read through the request's stream, one chunk per write and no further than the write gets, so the send-tracking
layer above counts it as it would on a real network. Each send is recorded, and :attr:`FaultyTransport.calls` reads
it as a frozen :class:`FaultCall`: the step, the request line and headers, whether it connected, how many bytes it
wrote, and the response it returned, if any, with whether that was closed.
"""

import math
from collections.abc import AsyncIterator, Iterable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Final, final, override

import h2.errors
import h2.events
import httpcore2
import httpx2

from headgate._core import (
    CallContext,
    check_headers,
    check_int,
    check_kind,
    check_number,
    check_status,
    current_timing,
    refuse_subclass,
    sealed,
)
from headgate._request_body import encoded_json
from headgate.retry._tracking import head_size

__all__ = [
    "ConnectFail",
    "DropMidBody",
    "FaultCall",
    "FaultyTransport",
    "PoolFull",
    "RefusedStream",
    "Respond",
    "Slow",
    "Step",
]


@final
@sealed("FaultyTransport acts a step out by its class and fields: set its fields, or schedule another step")
@dataclass(frozen=True, slots=True)
class ConnectFail:
    """Fail to connect: ``httpx2.ConnectError`` (or ``ConnectTimeout`` with ``timeout=True``); nothing is sent."""

    timeout: bool = False

    def __post_init__(self) -> None:
        """Refuse a ``timeout`` that is not a ``bool``."""
        check_kind("timeout", self.timeout, bool, "a bool")


@final
@sealed("FaultyTransport acts a step out by its class and fields: set its fields, or schedule another step")
@dataclass(frozen=True, slots=True)
class PoolFull:
    """Time out waiting for a pooled connection: ``httpx2.PoolTimeout``; nothing is sent."""


#: What ``Respond(json=)`` takes, and what to write for anything else.
_JSON_TAKES: Final = "json= takes builtins: dict, list, tuple, str, int, float, bool and None"
_JSON_INSTEAD: Final = "convert the value with a flavor's to_builtins, or pass its encoded bytes as content="


@final
@dataclass(frozen=True, slots=True)
class Respond:
    """Answer with ``status_code``, ``headers``, and a body: ``json``, sent as ``application/json``, or ``content``.

    ``status_code`` is an HTTP status, 100 to 599: any other raises ``ValueError``. ``retry_after`` adds a
    ``Retry-After`` header with that many seconds, in plain digits: whole seconds as RFC 9110's delta-seconds, and a
    fraction as a decimal, which headgate reads too. A negative or non-finite one raises ``ValueError``, and so does
    one beside a ``Retry-After`` in ``headers``. ``json`` must be builtins JSON can carry, as a :class:`~headgate.Json`
    body's value must: anything else raises ``TypeError``, and ``NaN``, an infinity or a value that holds itself
    ``ValueError``, where the step is built. It is encoded each time the step answers, and sent as the ``Content-Type``
    that ``headers`` sets, if it sets one (``application/problem+json``, say). A body is given one way: a ``json``
    beside a ``content`` raises ``ValueError``.
    """

    status_code: int = 200
    headers: Mapping[str, str] = field(default_factory=dict)
    json: object = None
    content: bytes = b""
    retry_after: float | None = None

    def __post_init__(self) -> None:
        """Refuse a field of the wrong kind, and a ``retry_after`` no ``Retry-After`` can say: negative or infinite."""
        check_status("status_code", self.status_code, "a status, an int")
        check_headers("headers", self.headers)
        check_kind("content", self.content, bytes, "the body's bytes")
        if self.json is not None:
            if self.content:
                msg = (
                    "Respond takes a body as json= or as content=, not both: json= is encoded and sent as "
                    "application/json, content= is sent as it is"
                )
                raise ValueError(msg)
            _json_body(self.json)  # refused where the step is built, not when it answers
        if self.retry_after is not None:
            check_number("retry_after", self.retry_after, "seconds, a number, or None")
        if self.retry_after is not None and not (math.isfinite(self.retry_after) and self.retry_after >= 0):
            msg = f"retry_after must be a finite, non-negative number of seconds, got {self.retry_after!r}"
            raise ValueError(msg)
        if self.retry_after is not None and any(name.lower() == "retry-after" for name in self.headers):
            msg = (
                "Respond takes a Retry-After as retry_after= or in headers=, not both: retry_after= writes it from "
                "seconds, headers= sends it as given"
            )
            raise ValueError(msg)

    def __init_subclass__(cls, **kwargs: object) -> None:
        """Refuse a subclass: the fakes build the reply from the fields, and would call no override."""
        del kwargs
        refuse_subclass(
            cls,
            Respond,
            "set status_code, headers, json, content and retry_after instead, or answer with a function in a "
            "FakeTransport route",
        )


@final
@sealed("FaultyTransport acts a step out by its class and fields: set its fields, or schedule another step")
@dataclass(frozen=True, slots=True)
class DropMidBody:
    """Connect, write ``after_bytes`` of the request (head first, then body), then lose the connection.

    ``httpx2.WriteError`` when the request was cut short, ``httpx2.RemoteProtocolError`` (the server disconnecting
    without answering) when all of it was written. ``after_bytes=0`` drops the connection before anything is written;
    any more, and the whole head is reported written, as httpx2's trace reports it when the head's write starts. The
    body is read only as far as the write gets, so the chunks past the drop are never taken.
    """

    after_bytes: int

    def __post_init__(self) -> None:
        """Refuse a count that is not an ``int`` (``TypeError``), and a negative one (``ValueError``)."""
        wanted = "a number of bytes, an int of 0 or more"
        check_int("after_bytes", self.after_bytes, wanted)
        if self.after_bytes < 0:
            msg = f"after_bytes= takes {wanted} (got {self.after_bytes})"
            raise ValueError(msg)


@final
@sealed("FaultyTransport acts a step out by its class and fields: set its fields, or schedule another step")
@dataclass(frozen=True, slots=True)
class Slow:
    """Write the whole request, wait ``seconds`` for the server, then take step ``then``.

    The wait honors the request's read timeout: past it, the send raises ``httpx2.ReadTimeout`` after waiting the
    timeout, so ``seconds=math.inf`` is a server that never answers. A negative or NaN wait raises ``ValueError``.
    """

    seconds: float
    then: Respond = field(default_factory=Respond)

    def __post_init__(self) -> None:
        """Refuse a wait that is not a number or below 0, and a step to take after it that is not a :class:`Respond`."""
        wanted = "seconds, a number of 0 or more"
        check_number("seconds", self.seconds, wanted)
        if not self.seconds >= 0:
            msg = f"seconds= takes {wanted} (got {self.seconds})"
            raise ValueError(msg)
        check_kind("then", self.then, Respond, "a Respond")


@final
@sealed("FaultyTransport acts a step out by its class and fields: set its fields, or schedule another step")
@dataclass(frozen=True, slots=True)
class RefusedStream:
    """Write the request head, then have the server reset the HTTP/2 stream with ``REFUSED_STREAM``.

    Raised exactly as httpx2 raises it: ``httpx2.RemoteProtocolError`` chained from httpcore2's, whose argument is the
    h2 ``StreamReset`` event.
    """


type Step = ConnectFail | PoolFull | Respond | DropMidBody | Slow | RefusedStream
"""One scheduled answer to a send."""

#: The classes a :data:`Step` is one of, and what a schedule takes, for the refusal of anything else.
_STEPS: Final = (ConnectFail, PoolFull, Respond, DropMidBody, Slow, RefusedStream)
_SCHEDULE_WANTED: Final = "a list of steps, such as [ConnectFail(), Respond(200)]"


@final
@sealed("read what FaultyTransport.calls returns")
@dataclass(frozen=True, slots=True)
class FaultCall:
    """One send :class:`FaultyTransport` answered, as it stood when read: the step, what was sent, what came back.

    ``bytes_sent`` counts the head and the body bytes written; ``response_closed`` is ``None`` when the send returned
    no response, else whether that response had been closed. A send goes on after it is read (its response is closed
    later), so read :attr:`FaultyTransport.calls` again to see it as it stands then. Each read holds a copy of the
    headers sent, so editing one changes neither the log nor another read.
    """

    step: Step
    method: str
    url: str
    headers: httpx2.Headers
    connected: bool = False
    bytes_sent: int = 0
    response: httpx2.Response | None = None
    response_closed: bool | None = None
    error: Exception | None = None


@dataclass(slots=True)
class _Send:
    """One send as :class:`FaultyTransport` fills it in, with the fields of the :class:`FaultCall` it is read as."""

    step: Step
    method: str
    url: str
    headers: httpx2.Headers
    connected: bool = False
    bytes_sent: int = 0
    response: httpx2.Response | None = None
    response_closed: bool | None = None
    error: Exception | None = None

    def as_read(self) -> FaultCall:
        """Return this send as it stands now, frozen, with headers of its own."""
        return FaultCall(
            step=self.step,
            method=self.method,
            url=self.url,
            headers=self.headers.copy(),
            connected=self.connected,
            bytes_sent=self.bytes_sent,
            response=self.response,
            response_closed=self.response_closed,
            error=self.error,
        )


class _TrackedBody(httpx2.AsyncByteStream):
    """A response body that records in its send when it is closed."""

    def __init__(self, content: bytes, call: _Send) -> None:
        """Serve *content*, recording the close in *call*."""
        self._content = content
        self._call = call
        call.response_closed = False

    @override
    async def __aiter__(self) -> AsyncIterator[bytes]:
        if self._content:
            yield self._content

    @override
    async def aclose(self) -> None:
        """Record the close."""
        self._call.response_closed = True


def _seconds(value: float) -> str:
    """Return *value* in plain decimal digits, never with an exponent: an integer when it is whole."""
    if float(value).is_integer():
        return str(int(value))
    return format(Decimal(repr(float(value))), "f")


def _refused_stream(request: httpx2.Request) -> httpx2.RemoteProtocolError:
    """Return the error httpx2 raises for an HTTP/2 stream the server reset with ``REFUSED_STREAM``."""
    event = h2.events.StreamReset(stream_id=1, error_code=h2.errors.ErrorCodes.REFUSED_STREAM)
    cause = httpcore2.RemoteProtocolError(event)
    error = httpx2.RemoteProtocolError(str(cause), request=request)
    error.__cause__ = cause
    return error


def _read_timeout(request: httpx2.Request) -> float | None:
    """Return the read timeout *request* carries, or ``None`` when it has none."""
    timeout = request.extensions.get("timeout")
    read = timeout.get("read") if isinstance(timeout, dict) else None
    return read if isinstance(read, float | int) else None


@dataclass(slots=True)
class _Write:
    """How much of a request a send wrote, and whether that was all of it."""

    written: int = 0
    complete: bool = True


@final
@sealed("schedule the steps it takes, or wrap it in a transport of your own")
class FaultyTransport(httpx2.AsyncBaseTransport):
    """Answer each send with the next step of ``schedule``, and record it in :attr:`calls`. See the module docstring.

    A :class:`Slow` step waits through the sleep of the timing current when the transport is built
    (:func:`headgate.timing.current_timing`), so one built inside ``TestClock.install()`` waits on the test clock. A
    send past the end of the schedule raises ``AssertionError``.
    """

    def __init__(self, *, schedule: Iterable[Step]) -> None:
        """Answer sends with *schedule*, in order, waiting through the current timing's sleep.

        :raises TypeError: for a *schedule* that is not an iterable of steps, naming what it holds that is not one.
        """
        check_kind("schedule", schedule, Iterable, _SCHEDULE_WANTED)
        self._schedule = list(schedule)
        for step in self._schedule:
            check_kind("schedule", step, _STEPS, _SCHEDULE_WANTED)
        self._sleep = current_timing().sleep
        self._calls: list[_Send] = []

    @property
    def calls(self) -> tuple[FaultCall, ...]:
        """Every send so far, in order, each as it stands now: the step it took, and what it sent and got."""
        return tuple(call.as_read() for call in self._calls)

    @override
    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        if len(self._calls) >= len(self._schedule):
            msg = f"FaultyTransport schedule exhausted after {len(self._schedule)} sends"
            raise AssertionError(msg)
        step = self._schedule[len(self._calls)]
        call = _Send(step, request.method, str(request.url), request.headers.copy())
        self._calls.append(call)
        try:
            return await self._take(step, request, call)
        except Exception as exc:
            call.error = exc
            raise

    async def _take(self, step: Step, request: httpx2.Request, call: _Send) -> httpx2.Response:
        """Act out *step* for *request*, recording it in *call*."""
        match step:
            case ConnectFail(timeout=timeout):
                kind = httpx2.ConnectTimeout if timeout else httpx2.ConnectError
                msg = "scheduled connect failure"
                raise kind(msg, request=request)
            case PoolFull():
                msg = "scheduled pool timeout"
                raise httpx2.PoolTimeout(msg, request=request)
            case DropMidBody(after_bytes=after_bytes):
                write = await _write(request, call, limit=after_bytes)
                if write.complete:
                    msg = "Server disconnected without sending a response."
                    raise httpx2.RemoteProtocolError(msg, request=request)
                msg = "scheduled connection drop while writing"
                raise httpx2.WriteError(msg, request=request)
            case RefusedStream():
                await _write(request, call, body=False)
                raise _refused_stream(request)
            case Slow(seconds=seconds, then=then):
                await _write(request, call)
                read = _read_timeout(request)
                if read is not None and seconds > read:
                    await self._sleep(read)
                    msg = "scheduled read timeout"
                    raise httpx2.ReadTimeout(msg, request=request)
                await self._sleep(seconds)
                return _respond(then, request, call)
            case _:
                await _write(request, call)
                return _respond(step, request, call)


async def _write(request: httpx2.Request, call: _Send, *, limit: int | None = None, body: bool = True) -> _Write:
    """Connect and write at most *limit* bytes of *request*, head first, then its body when *body*; record it in *call*.

    The connection and the head are reported on the call's context the way httpx2's trace reports them: the whole head
    is counted once its write starts, even if the connection drops inside it, and nothing when *limit* is 0. The body
    is read through the request's stream one chunk per write, as a network write reads it, so a counting layer above
    counts each chunk taken; the first chunk the connection drops inside is the last one taken.
    """
    context = CallContext.find(request)
    progress = None if context is None else context.progress
    call.connected = True
    if progress is not None:
        progress.mark_connected()
    if limit == 0:
        return _Write(written=0, complete=False)
    head = head_size(request)
    if progress is not None:
        progress.add_sent(head)
    write = _Write(written=head if limit is None else min(head, limit), complete=limit is None or head <= limit)
    stream = request.stream
    if body and write.complete and isinstance(stream, httpx2.AsyncByteStream):
        async for chunk in stream:
            take = len(chunk) if limit is None else min(len(chunk), limit - write.written)
            write.written += take
            if take < len(chunk):
                write.complete = False
                break
    call.bytes_sent += write.written
    return write


def response_parts(step: Respond) -> tuple[list[tuple[str, str]], bytes]:
    """Return the headers and the body *step* answers with, in :class:`FaultyTransport` and ``FakeTransport`` alike.

    Headers given as an ``httpx2.Headers`` keep each line, as a response may repeat one (``Set-Cookie``).
    """
    given = step.headers
    headers = list(given.multi_items() if isinstance(given, httpx2.Headers) else given.items())
    if step.retry_after is not None:
        headers.append(("Retry-After", _seconds(step.retry_after)))
    if step.json is None:
        return headers, step.content
    if all(name.lower() != "content-type" for name, _ in headers):
        headers.append(("Content-Type", "application/json"))
    return headers, _json_body(step.json)


def _json_body(value: object) -> bytes:
    """Return *value* encoded as a :class:`Respond` sends it: ``json.dumps``'s spelling, as the fakes always sent it.

    :raises TypeError: for a value that is not builtins JSON can carry.
    :raises ValueError: for ``NaN``, an infinity, or a value that holds itself.
    """
    return encoded_json(value, subject="json=", takes=_JSON_TAKES, instead=_JSON_INSTEAD, compact=False)


def _respond(step: Respond, request: httpx2.Request, call: _Send) -> httpx2.Response:
    """Return the response *step* describes, with a body that records its close in *call*."""
    headers, content = response_parts(step)
    response = httpx2.Response(step.status_code, headers=headers, stream=_TrackedBody(content, call), request=request)
    call.response = response
    return response
