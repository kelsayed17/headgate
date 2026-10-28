# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""The ``text/event-stream`` grammar (WHATWG HTML, "Server-sent events"), parsed from bytes as they arrive.

:class:`EventParser` is what :meth:`Stream.aiter_events <headgate.streaming.Stream.aiter_events>` and the buffered
:func:`~headgate.event_stream` reader share. It follows the grammar where httpx2's ``EventSource`` departs from it: the
stream is UTF-8 whatever its ``charset`` says, one leading byte order mark is skipped, a block whose data buffer is
empty (``event: ping``, ``retry:`` or ``id:`` alone) dispatches nothing, and ``retry:`` takes only ASCII digits. Each
event is still an ``httpx2.ServerSentEvent``, carrying the ``retry`` its own block named, as httpx2's does.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Final

import httpx2

if TYPE_CHECKING:
    from collections.abc import Iterator

__all__ = ["EventParser", "EventTooLargeError"]

#: A line ending: CRLF, a lone LF or a lone CR. CR and LF never occur inside a UTF-8 sequence, so lines split as bytes.
_LINE_END: Final = re.compile(rb"\r\n|\r|\n")

#: The UTF-8 byte order mark, skipped once at the start of the stream.
_BOM: Final = b"\xef\xbb\xbf"


class EventTooLargeError(Exception):
    """An event outgrew the parser's ``max_event_size``: the caller maps it onto its own error."""


class EventParser:
    """An incremental event-stream parser: :meth:`feed` it the body's bytes, in chunks of any size, for its events.

    *max_event_size* bounds the bytes one event's field lines hold (comments and line endings aside), counting a line
    not yet ended; past it, :meth:`feed` raises :class:`EventTooLargeError`. ``None`` sets no bound. An event that no
    blank line ends when the body does is dropped, as the grammar asks.
    """

    __slots__ = (
        "_after_cr",
        "_data",
        "_first_line",
        "_id",
        "_line",
        "_max_event_size",
        "_retry",
        "_size",
        "_type",
        "last_event_id",
    )

    def __init__(self, max_event_size: int | None = None) -> None:
        """Parse events of at most *max_event_size* bytes, or of any size for ``None``."""
        self._max_event_size = max_event_size
        self._line = bytearray()
        self._after_cr = False
        self._first_line = True
        self._size = 0
        self._type = ""
        self._data: list[str] = []
        self._id: str | None = None
        self._retry: int | None = None
        #: The last event ID as of the last block ended (``""`` once the server reset it), ``None`` before any id.
        self.last_event_id: str | None = None

    def feed(self, chunk: bytes) -> Iterator[httpx2.ServerSentEvent]:
        """Yield each event *chunk* completes, in order, updating :attr:`last_event_id` as each block ends."""
        start = 0
        if self._after_cr and chunk:
            self._after_cr = False
            start = 1 if chunk.startswith(b"\n") else 0
        for match in _LINE_END.finditer(chunk, start):
            self._line += chunk[start : match.start()]
            start = match.end()
            self._after_cr = start == len(chunk) and match.group() == b"\r"
            event = self._end_line()
            if event is not None:
                yield event
        self._line += chunk[start:]
        if self._max_event_size is not None and self._size + len(self._line) > self._max_event_size:
            raise EventTooLargeError

    def _end_line(self) -> httpx2.ServerSentEvent | None:
        """Process the line just ended; return the event it dispatches, if it is a blank line ending one."""
        raw = bytes(self._line)
        self._line.clear()
        if self._first_line:
            self._first_line = False
            raw = raw.removeprefix(_BOM)
        if not raw:
            return self._dispatch()
        if raw.startswith(b":"):
            return None
        self._size += len(raw)
        if self._max_event_size is not None and self._size > self._max_event_size:
            raise EventTooLargeError
        name, _, value = raw.decode("utf-8", "replace").partition(":")
        value = value.removeprefix(" ")
        match name:
            case "event":
                self._type = value
            case "data":
                self._data.append(value)
            case "id" if "\0" not in value:
                self._id = value
            case "retry" if value.isascii() and value.isdigit():
                self._retry = int(value)
        return None

    def _dispatch(self) -> httpx2.ServerSentEvent | None:
        """End the block: keep its id as :attr:`last_event_id` and return its event, or ``None`` for no data."""
        self.last_event_id = self._id
        data, event_type, retry = self._data, self._type, self._retry
        self._data, self._type, self._retry, self._size = [], "", None, 0
        if not data:
            return None
        return httpx2.ServerSentEvent(
            event=event_type or "message", data="\n".join(data), id=self._id or "", retry=retry
        )
