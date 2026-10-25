# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""Fault injection: a network transport that fails on a schedule, and records what every attempt sent.

:class:`FaultyTransport` stands in for the network (``HttpOptions(transport=FaultyTransport(...))``) and answers each
send with the next step of its schedule::

    FaultyTransport(schedule=[ConnectFail(), Respond(503, retry_after=1), DropMidBody(after_bytes=100),
                              Slow(seconds=2), RefusedStream(), Respond(200, json={"ok": True})])

It reports each send's progress the way httpx2's own transport does through its trace: a connection obtained marks
the call's current attempt connected, and the request head is counted as written when it starts to go out. The body
is read through the request's stream, so the send-tracking layer above counts it as it would on a real network. Each
send is recorded in :attr:`FaultyTransport.calls` as a :class:`FaultCall`: the step, the request line and headers,
whether it connected, how many bytes it wrote, and the response it returned, if any, with whether that was closed.
"""

from __future__ import annotations

import asyncio
import json as jsonlib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final, override

import h2.errors
import h2.events
import httpcore2
import httpx2

from headgate._core import CallContext

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Mapping

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

#: Bytes an HTTP/1.1 head spends around each header line (``": "`` and CRLF), and around the request line.
_LINE_OVERHEAD: Final = 4


@dataclass(frozen=True, slots=True)
class ConnectFail:
    """Fail to connect: ``httpx2.ConnectError`` (or ``ConnectTimeout`` with ``timeout=True``); nothing is sent."""

    timeout: bool = False


@dataclass(frozen=True, slots=True)
class PoolFull:
    """Time out waiting for a pooled connection: ``httpx2.PoolTimeout``; nothing is sent."""


@dataclass(frozen=True, slots=True)
class Respond:
    """Answer with ``status``, ``headers``, and a body: ``json`` encoded if given, else ``content``.

    ``retry_after`` adds a ``Retry-After`` header with that many seconds.
    """

    status: int = 200
    headers: Mapping[str, str] = field(default_factory=dict)
    json: object = None
    content: bytes = b""
    retry_after: float | None = None

    def response(self) -> tuple[list[tuple[str, str]], bytes]:
        """Return the headers and the body this step answers with."""
        headers = list(self.headers.items())
        if self.retry_after is not None:
            headers.append(("Retry-After", f"{self.retry_after:g}"))
        if self.json is None:
            return headers, self.content
        headers.append(("Content-Type", "application/json"))
        return headers, jsonlib.dumps(self.json).encode()


@dataclass(frozen=True, slots=True)
class DropMidBody:
    """Connect, write ``after_bytes`` of the request (head first, then body), then lose the connection.

    ``httpx2.WriteError`` when the request was cut short, ``httpx2.RemoteProtocolError`` (the server disconnecting
    without answering) when all of it was written. ``after_bytes=0`` drops the connection before anything is written.
    """

    after_bytes: int


@dataclass(frozen=True, slots=True)
class Slow:
    """Write the whole request, wait ``seconds`` for the server, then take step ``then``.

    The wait honours the request's read timeout: past it, the send raises ``httpx2.ReadTimeout`` after waiting the
    timeout.
    """

    seconds: float
    then: Respond = field(default_factory=Respond)


@dataclass(frozen=True, slots=True)
class RefusedStream:
    """Write the request head, then have the server reset the HTTP/2 stream with ``REFUSED_STREAM``.

    Raised exactly as httpx2 raises it: ``httpx2.RemoteProtocolError`` chained from httpcore2's, whose argument is the
    h2 ``StreamReset`` event.
    """


type Step = ConnectFail | PoolFull | Respond | DropMidBody | Slow | RefusedStream
"""One scheduled answer to a send."""


@dataclass(slots=True)
class FaultCall:
    """One send :class:`FaultyTransport` answered: the step taken, what was sent, and what came back.

    ``bytes_sent`` counts the head and the body bytes written; ``response_closed`` is ``None`` when the send returned
    no response, else whether that response has been closed.
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


class _TrackedBody(httpx2.AsyncByteStream):
    """A response body that records in its :class:`FaultCall` when it is closed."""

    def __init__(self, content: bytes, call: FaultCall) -> None:
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


def head_size(request: httpx2.Request) -> int:
    """Return the size of *request*'s head as HTTP/1.1 writes it: the request line and every header line."""
    size = len(request.method) + len(request.url.raw_path) + len(b"HTTP/1.1") + _LINE_OVERHEAD
    return size + sum(len(name) + len(value) + _LINE_OVERHEAD for name, value in request.headers.raw)


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


class FaultyTransport(httpx2.AsyncBaseTransport):
    """Answer each send with the next step of ``schedule``, and record it in :attr:`calls`. See the module docstring.

    *sleep* is what a :class:`Slow` step waits through; a test passes a fake clock's sleep to control time. A send past
    the end of the schedule raises ``AssertionError``.
    """

    def __init__(self, *, schedule: list[Step], sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
        """Answer sends with *schedule*, in order, waiting through *sleep*."""
        self._schedule = list(schedule)
        self._sleep = sleep
        self.calls: list[FaultCall] = []

    @override
    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        if len(self.calls) >= len(self._schedule):
            msg = f"FaultyTransport schedule exhausted after {len(self._schedule)} sends"
            raise AssertionError(msg)
        step = self._schedule[len(self.calls)]
        call = FaultCall(step, request.method, str(request.url), request.headers.copy())
        self.calls.append(call)
        try:
            return await self._take(step, request, call)
        except Exception as exc:
            call.error = exc
            raise

    async def _take(self, step: Step, request: httpx2.Request, call: FaultCall) -> httpx2.Response:
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
                await _write(request, call, limit=head_size(request))
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
            case Respond():
                await _write(request, call)
                return _respond(step, request, call)


async def _write(request: httpx2.Request, call: FaultCall, *, limit: int | None = None) -> _Write:
    """Connect and write at most *limit* bytes of *request* (head first, then body), recording it in *call*.

    The connection and the head are reported on the call's context the way httpx2's trace reports them; the body is
    read through the request's stream, in full, as a network write would read it, so a counting layer above sees it.
    """
    context = CallContext.find(request)
    progress = None if context is None else context.progress
    call.connected = True
    head = head_size(request)
    write = _Write(written=head if limit is None else min(head, limit), complete=limit is None or head <= limit)
    if progress is not None:
        progress.mark_connected()
        progress.add_sent(write.written)
    stream = request.stream
    if isinstance(stream, httpx2.AsyncByteStream):
        async for chunk in stream:
            take = len(chunk) if limit is None else max(0, min(len(chunk), limit - write.written))
            write.complete = write.complete and take == len(chunk)
            write.written += take
    call.bytes_sent += write.written
    return write


def _respond(step: Respond, request: httpx2.Request, call: FaultCall) -> httpx2.Response:
    """Return the response *step* describes, with a body that records its close in *call*."""
    headers, content = step.response()
    response = httpx2.Response(step.status, headers=headers, stream=_TrackedBody(content, call), request=request)
    call.response = response
    return response
