# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""The open response :meth:`AsyncBaseClient.stream <headgate.AsyncBaseClient.stream>` hands its block.

Its iterators read the body as it arrives, after content-decoding, and map a failure mid-body onto the
``ClientError`` tree where it happens, so a caller catching ``TransportError`` around its loop catches it. The
mapping (and the breaker charge that goes with a network failure) is the client's, passed in as *fail*.
"""

from __future__ import annotations

import codecs
import functools
from typing import TYPE_CHECKING, Final, Protocol, final

from headgate._failures import parse_error
from headgate.errors import ResponseTooLargeError
from headgate.retry._policy import NETWORK_ERRORS
from headgate.streaming._events import EventParser, EventTooLargeError

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Iterator

    import httpx2

    from headgate._failures import CallSite
    from headgate.errors import ClientError

__all__ = ["DEFAULT_MAX_EVENT_SIZE", "NetworkFailure", "OpenStream", "Stream"]

type NetworkFailure = Callable[[httpx2.HTTPError | OSError], ClientError]
"""Turns a network failure mid-body into the ``ClientError`` to raise, charging the origin's breaker."""

#: The largest server-sent event :meth:`Stream.aiter_events` buffers by default: httpx2's own default, 1 MiB.
DEFAULT_MAX_EVENT_SIZE: Final = 1024 * 1024

#: Every character ``str.splitlines`` ends a line at, as httpx2's ``aiter_lines`` does.
_LINE_ENDS: Final = "\n\r\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029"

#: The media type an event stream must declare.
_EVENT_STREAM: Final = "text/event-stream"


class Stream(Protocol):
    """A response whose head has arrived and whose body is read as it arrives, inside ``client.stream(...)``.

    The status is a 2xx (anything else raised before the block was entered) and the client's ``response_check`` has
    passed. Read the body once, with one of :meth:`aiter_bytes`, :meth:`aiter_lines` or :meth:`aiter_events`; the
    response is closed when the block exits, whether or not it was read to the end. A network failure or timeout while
    reading raises ``TransportError`` / ``ClientTimeoutError`` from the iteration.
    """

    @property
    def status_code(self) -> int:
        """The response's status."""
        ...

    @property
    def headers(self) -> httpx2.Headers:
        """The response's headers."""
        ...

    @property
    def request_id(self) -> str | None:
        """The upstream's request id, from the client's ``request_id_headers``, bounded; ``None`` without one."""
        ...

    @property
    def last_event_id(self) -> str | None:
        """The event id the server last set, as of what :meth:`aiter_events` has read; ``None`` before it sets one.

        It is ``""`` once the server reset it with an empty ``id``, and a block without data (an ``id:`` alone) sets
        it too, as a browser's ``EventSource`` keeps it. Pass it as ``last_event_id=`` when re-opening the stream, so
        the server resumes after it.
        """
        ...

    @property
    def retry(self) -> int | None:
        """The reconnection time in milliseconds the server last set, as of what :meth:`aiter_events` has read.

        ``None`` before a ``retry:`` sets one. A ``retry:`` alone, in a block without data, sets it too, and it holds
        until another replaces it, as a browser's ``EventSource`` keeps it; an event's own ``retry`` is only the field
        its block named. Wait this long before re-opening the stream.
        """
        ...

    def aiter_bytes(self, chunk_size: int | None = None) -> AsyncIterator[bytes]:
        """Iterate the body as bytes, in chunks as they arrive or of *chunk_size* bytes."""
        ...

    def aiter_lines(self, *, max_line_size: int | None = DEFAULT_MAX_EVENT_SIZE) -> AsyncIterator[str]:
        """Iterate the body as decoded text lines, without their line endings, split as ``str.splitlines`` splits.

        A line past *max_line_size* characters (1 MiB by default, as an event; ``None`` for no limit) raises
        ``ResponseTooLargeError``, before it ends if it never does. The body is decoded by its ``charset``; one that is
        no text encoding (``base64``, ``rot13``) is read as UTF-8, as no ``charset`` is, and a body its ``charset``
        cannot decode raises ``ResponseParseError``.
        """
        ...

    def aiter_events(
        self, *, max_event_size: int | None = DEFAULT_MAX_EVENT_SIZE
    ) -> AsyncIterator[httpx2.ServerSentEvent]:
        """Iterate the body as server-sent events, each parsed as soon as its blank line arrives.

        Each event is an ``httpx2.ServerSentEvent`` carrying ``event``, ``data``, ``id`` and the ``retry`` its block
        named, parsed by the WHATWG grammar: the body is UTF-8 whatever its ``charset`` says, a leading byte order mark
        is skipped, and a block without data (an ``event: ping`` heartbeat, a ``retry:`` or ``id:`` alone) is not an
        event. An event past *max_event_size* bytes (``None`` for no limit) raises ``ResponseTooLargeError``.

        :raises ResponseParseError: at once, for a response that does not declare ``text/event-stream``.
        """
        ...


@final
class OpenStream:
    """The :class:`Stream` :meth:`AsyncBaseClient.stream <headgate.AsyncBaseClient.stream>` opens."""

    __slots__ = ("_events", "_fail", "_request_id", "_response", "_site")

    def __init__(self, response: httpx2.Response, site: CallSite, fail: NetworkFailure) -> None:
        """Wrap the streamed *response* of the call at *site*, mapping a mid-body failure through *fail*."""
        self._response = response
        self._site = site
        self._fail = fail
        self._request_id = site.details(response).get("request_id")
        self._events: EventParser | None = None

    @property
    def status_code(self) -> int:
        """The response's status."""
        return self._response.status_code

    @property
    def headers(self) -> httpx2.Headers:
        """The response's headers."""
        return self._response.headers

    @property
    def request_id(self) -> str | None:
        """The upstream's request id, from the client's ``request_id_headers``, bounded; ``None`` without one."""
        return self._request_id

    @property
    def last_event_id(self) -> str | None:
        """The event id the server last set, as of what :meth:`aiter_events` has read; ``None`` before it sets one.

        It is ``""`` once the server reset it with an empty ``id``, and a block without data (an ``id:`` alone) sets
        it too, as a browser's ``EventSource`` keeps it. Pass it as ``last_event_id=`` when re-opening the stream, so
        the server resumes after it.
        """
        return None if self._events is None else self._events.last_event_id

    @property
    def retry(self) -> int | None:
        """The reconnection time in milliseconds the server last set, as of what :meth:`aiter_events` has read.

        ``None`` before a ``retry:`` sets one. A ``retry:`` alone, in a block without data, sets it too, and it holds
        until another replaces it, as a browser's ``EventSource`` keeps it; an event's own ``retry`` is only the field
        its block named. Wait this long before re-opening the stream.
        """
        return None if self._events is None else self._events.retry

    def aiter_bytes(self, chunk_size: int | None = None) -> AsyncIterator[bytes]:
        """Iterate the body as bytes, in chunks as they arrive or of *chunk_size* bytes."""
        return _Guarded(self._response.aiter_bytes(chunk_size), self._fail)

    def aiter_lines(self, *, max_line_size: int | None = DEFAULT_MAX_EVENT_SIZE) -> AsyncIterator[str]:
        """Iterate the body as decoded text lines, without their line endings, split as ``str.splitlines`` splits.

        A line past *max_line_size* characters (1 MiB by default, as an event; ``None`` for no limit) raises
        ``ResponseTooLargeError``, before it ends if it never does. The body is decoded by its ``charset``; one that is
        no text encoding (``base64``, ``rot13``) is read as UTF-8, as no ``charset`` is, and a body its ``charset``
        cannot decode raises ``ResponseParseError``.
        """
        decoder = _text_decoder(self._response.encoding)
        chunks = _Guarded(self._response.aiter_bytes(), self._fail)
        unreadable = functools.partial(parse_error, resp=self._response, site=self._site)
        too_large = functools.partial(self._too_large, f"line over {max_line_size} characters")
        return _lines(chunks, decoder, max_line_size, unreadable, too_large)

    def aiter_events(
        self, *, max_event_size: int | None = DEFAULT_MAX_EVENT_SIZE
    ) -> AsyncIterator[httpx2.ServerSentEvent]:
        """Iterate the body as server-sent events, each parsed as soon as its blank line arrives.

        Each event is an ``httpx2.ServerSentEvent`` carrying ``event``, ``data``, ``id`` and the ``retry`` its block
        named, parsed by the WHATWG grammar: the body is UTF-8 whatever its ``charset`` says, a leading byte order mark
        is skipped, and a block without data (an ``event: ping`` heartbeat, a ``retry:`` or ``id:`` alone) is not an
        event. An event past *max_event_size* bytes (``None`` for no limit) raises ``ResponseTooLargeError``.

        :raises ResponseParseError: at once, for a response that does not declare ``text/event-stream``.
        """
        media_type = self._response.headers.get("content-type", "").partition(";")[0].strip().lower()
        if media_type != _EVENT_STREAM:
            error = ValueError(f"expected an event stream, got content type {media_type or 'none'!r}")
            raise parse_error(error, self._response, self._site)
        self._events = EventParser(max_event_size)
        chunks = _Guarded(self._response.aiter_bytes(), self._fail)
        too_large = functools.partial(self._too_large, f"server-sent event over {max_event_size} bytes")
        return _Events(chunks, self._events, too_large)

    def _too_large(self, what: str) -> ClientError:
        """Return the error for *what* outgrew its limit: the body's fault, so it charges no breaker."""
        message = f"{what} from {self._site.method} {self._site.url}"
        return ResponseTooLargeError(message, **self._site.details(self._response))


def _text_decoder(encoding: str | None) -> codecs.IncrementalDecoder:
    """Return an incremental decoder for *encoding*, or UTF-8's when it decodes no text (``base64``, ``undefined``)."""
    name = encoding or "utf-8"
    try:
        # One byte, since a codec that is no text encoding refuses only a non-empty body.
        b"a".decode(name, "replace")
    except LookupError, UnicodeError:
        name = "utf-8"
    return codecs.getincrementaldecoder(name)("replace")


async def _lines(
    chunks: AsyncIterator[bytes],
    decoder: codecs.IncrementalDecoder,
    max_line_size: int | None,
    unreadable: Callable[[Exception], ClientError],
    too_large: Callable[[], ClientError],
) -> AsyncIterator[str]:
    """Yield the lines *chunks* decode to, raising what *too_large* builds for one past *max_line_size* characters.

    shortcut: the line still arriving is re-split with each chunk, quadratic for a long one; it is bounded by
    *max_line_size* unless that is ``None``, so only an unbounded caller pays.
    """
    pending = ""
    ended = False
    while not ended:
        chunk = await anext(chunks, None)
        ended = chunk is None
        try:
            pending += decoder.decode(chunk or b"", final=ended)
        except UnicodeError as exc:
            raise unreadable(exc) from exc
        lines = pending.splitlines(keepends=True)
        # The last piece waits for more: a line not yet ended, or a CR that may be the first half of a CRLF.
        waiting = not ended and lines and (lines[-1][-1] not in _LINE_ENDS or lines[-1].endswith("\r"))
        pending = lines.pop() if waiting else ""
        for line in lines:
            text = line.splitlines()[0]
            if max_line_size is not None and len(text) > max_line_size:
                raise too_large()
            yield text
        if max_line_size is not None and len(pending.removesuffix("\r")) > max_line_size:
            raise too_large()


class _Guarded[T]:
    """An async iterator that maps a network failure from *source* through *fail*."""

    __slots__ = ("_fail", "_source")

    def __init__(self, source: AsyncIterator[T], fail: NetworkFailure) -> None:
        """Iterate *source*, raising what *fail* builds for a network failure."""
        self._source = source
        self._fail = fail

    def __aiter__(self) -> AsyncIterator[T]:
        """Return this iterator."""
        return self

    async def __anext__(self) -> T:
        """Return the next item, or raise the mapped failure."""
        try:
            return await anext(self._source)
        except NETWORK_ERRORS as exc:
            raise self._fail(exc) from exc


@final
class _Events:
    """The event iterator: the body's chunks fed through *parser*, raising what *too_large* builds for a large event."""

    __slots__ = ("_chunks", "_parser", "_pending", "_too_large")

    def __init__(self, chunks: AsyncIterator[bytes], parser: EventParser, too_large: Callable[[], ClientError]) -> None:
        """Parse *chunks* with *parser*, raising what *too_large* builds for an oversized event."""
        self._chunks = chunks
        self._parser = parser
        self._too_large = too_large
        self._pending: Iterator[httpx2.ServerSentEvent] = iter(())

    def __aiter__(self) -> AsyncIterator[httpx2.ServerSentEvent]:
        """Return this iterator."""
        return self

    async def __anext__(self) -> httpx2.ServerSentEvent:
        """Return the next event, reading chunks until one completes it."""
        while True:
            try:
                event = next(self._pending, None)
            except EventTooLargeError as exc:
                raise self._too_large() from exc
            if event is not None:
                return event
            self._pending = self._parser.feed(await anext(self._chunks))
