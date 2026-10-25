# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""Send tracking: record, as facts, whether an attempt obtained a connection and how many request bytes it wrote.

:class:`SendTrackingTransport` sits directly above the network transport (see :mod:`headgate._stack`) and fills the
current attempt's :class:`~headgate._core.SendProgress` from two sources:

* httpx2's ``trace`` request extension, which httpcore2 calls at each step of a send: a TCP or Unix-socket connection
  completing marks the attempt connected, and the request head starting to go out marks it connected and counts the
  head's size as written (conservatively, at the start of the write, since a failed write may still have put bytes on
  the wire);
* a counting wrapper around the request's body stream, which counts each body chunk as the transport takes it.

A transport that neither calls the trace nor reads the body stream (a custom or test transport) reports nothing, and
the retry layer falls back to judging by the exception's type.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final, override

import httpx2

from headgate._core import CallContext

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

    from headgate._core import SendProgress

__all__ = ["SendTrackingTransport"]

#: The request extension httpcore2 calls with each step of a send.
_TRACE: Final = "trace"

#: The trace steps that mean a new connection was obtained.
_CONNECTED_STEPS: Final = ("connect_tcp.complete", "connect_unix_socket.complete")

#: The trace step that means the request head is being written.
_HEAD_STEP: Final = "send_request_headers.started"

#: Bytes an HTTP/1.1 head spends around each header line (``": "`` and CRLF), and around the request line.
_LINE_OVERHEAD: Final = 4

type _Trace = Callable[[str, dict[str, object]], Awaitable[None]]


def head_size(request: httpx2.Request) -> int:
    """Return the size of *request*'s head as HTTP/1.1 writes it: the request line and every header line."""
    target = request.url.raw_path
    size = len(request.method) + len(target) + len(b"HTTP/1.1") + _LINE_OVERHEAD
    return size + sum(len(name) + len(value) + _LINE_OVERHEAD for name, value in request.headers.raw)


class _CountingStream(httpx2.AsyncByteStream):
    """A request body stream that counts each chunk into the attempt's progress as the transport takes it."""

    def __init__(self, stream: httpx2.AsyncByteStream, progress: SendProgress) -> None:
        """Count the chunks of *stream* into *progress*."""
        self._stream = stream
        self._progress = progress

    @override
    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for chunk in self._stream:
            self._progress.add_sent(len(chunk))
            yield chunk

    @override
    async def aclose(self) -> None:
        """Close the wrapped stream."""
        await self._stream.aclose()


def _tracer(request: httpx2.Request, progress: SendProgress, chained: _Trace | None) -> _Trace:
    """Return a trace callback recording *request*'s send into *progress*, then calling *chained* if there is one."""

    async def trace(name: str, info: dict[str, object]) -> None:
        if name.endswith(_CONNECTED_STEPS):
            progress.mark_connected()
        elif name.endswith(_HEAD_STEP):
            progress.mark_connected()
            progress.add_sent(head_size(request))
        if chained is not None:
            await chained(name, info)

    return trace


class SendTrackingTransport(httpx2.AsyncBaseTransport):
    """Record each send's connection and written bytes on the call's context. See the module docstring."""

    def __init__(self, *, transport: httpx2.AsyncBaseTransport) -> None:
        """Track the sends made through *transport*."""
        self._transport = transport

    @override
    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        context = CallContext.find(request)
        if context is None:
            return await self._transport.handle_async_request(request)
        progress = context.progress
        stream = request.stream
        chained = request.extensions.get(_TRACE)
        if isinstance(stream, httpx2.AsyncByteStream):
            request.stream = _CountingStream(stream, progress)
        request.extensions[_TRACE] = _tracer(request, progress, chained)
        try:
            return await self._transport.handle_async_request(request)
        finally:
            request.stream = stream
            if chained is None:
                del request.extensions[_TRACE]
            else:
                request.extensions[_TRACE] = chained

    @override
    async def aclose(self) -> None:
        """Close the wrapped transport."""
        await self._transport.aclose()
