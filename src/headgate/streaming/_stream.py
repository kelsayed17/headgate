# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""The open response :meth:`AsyncBaseClient.stream <headgate.AsyncBaseClient.stream>` hands its block.

Its iterators read the body as it arrives, after content-decoding, and map a failure mid-body onto the
``ClientError`` tree where it happens, so a caller catching ``TransportError`` around its loop catches it. The
mapping (and the breaker charge that goes with a network failure) is the client's, passed in as *fail*.
"""

from __future__ import annotations

import functools
from typing import TYPE_CHECKING, Final, final, override

import httpx2

from headgate._failures import parse_error
from headgate.errors import ResponseTooLargeError
from headgate.retry._policy import NETWORK_ERRORS

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from headgate._failures import CallSite
    from headgate.errors import ClientError

__all__ = ["DEFAULT_MAX_EVENT_SIZE", "NetworkFailure", "Stream"]

type NetworkFailure = Callable[[httpx2.HTTPError | OSError], ClientError]
"""Turns a network failure mid-body into the ``ClientError`` to raise, charging the host's breaker."""

#: The largest server-sent event :meth:`Stream.aiter_events` buffers by default: httpx2's own default, 1 MiB.
DEFAULT_MAX_EVENT_SIZE: Final = 1024 * 1024

#: The media type an event stream must declare.
_EVENT_STREAM: Final = "text/event-stream"


@final
class Stream:
    """A response whose head has arrived and whose body is read as it arrives, inside ``client.stream(...)``.

    The status is a 2xx (anything else raised before the block was entered) and the client's ``refusal_check`` has
    passed. Read the body once, with one of :meth:`aiter_bytes`, :meth:`aiter_lines` or :meth:`aiter_events`; the
    response is closed when the block exits, whether or not it was read to the end. A network failure or timeout while
    reading raises ``TransportError`` / ``ClientTimeoutError`` from the iteration.
    """

    __slots__ = ("_fail", "_last_event_id", "_request_id", "_response", "_site")

    def __init__(self, response: httpx2.Response, site: CallSite, fail: NetworkFailure) -> None:
        """Wrap the streamed *response* of the call at *site*, mapping a mid-body failure through *fail*."""
        self._response = response
        self._site = site
        self._fail = fail
        self._request_id = site.details(response).get("request_id")
        self._last_event_id: str | None = None

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
        """The last event id :meth:`aiter_events` has seen (``""`` once the server reset it), ``None`` before any.

        Pass it as ``last_event_id=`` when re-opening the stream, so the server resumes after it.
        """
        return self._last_event_id

    def aiter_bytes(self, chunk_size: int | None = None) -> AsyncIterator[bytes]:
        """Iterate the body as bytes, in chunks as they arrive or of *chunk_size* bytes."""
        return _Guarded(self._response.aiter_bytes(chunk_size), self._fail)

    def aiter_lines(self) -> AsyncIterator[str]:
        """Iterate the body as decoded text lines, without their line endings."""
        return _Guarded(self._response.aiter_lines(), self._fail)

    def aiter_events(
        self, *, max_event_size: int | None = DEFAULT_MAX_EVENT_SIZE
    ) -> AsyncIterator[httpx2.ServerSentEvent]:
        """Iterate the body as server-sent events, each parsed as soon as its blank line arrives.

        Each event carries ``event``, ``data``, ``id`` and ``retry`` as httpx2 parses them (the WHATWG grammar), and
        its ``id`` is kept as :attr:`last_event_id`. An event past *max_event_size* bytes (``None`` for no limit)
        raises ``ResponseTooLargeError``.

        :raises ResponseParseError: at once, for a response that does not declare ``text/event-stream``.
        """
        media_type = self._response.headers.get("content-type", "").partition(";")[0].strip().lower()
        if media_type != _EVENT_STREAM:
            error = ValueError(f"expected an event stream, got content type {media_type or 'none'!r}")
            raise parse_error(error, self._response, self._site)
        source = httpx2.EventSource(self._response, max_event_size=max_event_size)
        return _Events(aiter(source), functools.partial(self._event_failure, max_event_size=max_event_size), self._saw)

    def _event_failure(self, exc: httpx2.HTTPError | OSError, max_event_size: int | None) -> ClientError:
        """Map a failure while events arrive: an oversized event is the body's fault, anything else the network's."""
        if isinstance(exc, httpx2.SSEError):
            message = f"server-sent event over {max_event_size} bytes from {self._site.method} {self._site.url}"
            return ResponseTooLargeError(message, **self._site.details(self._response))
        return self._fail(exc)

    def _saw(self, event: httpx2.ServerSentEvent) -> None:
        """Record *event*'s id as the last one seen."""
        self._last_event_id = event.id


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
class _Events(_Guarded[httpx2.ServerSentEvent]):
    """The event iterator: a :class:`_Guarded` that also reports each event to *seen*."""

    __slots__ = ("_seen",)

    def __init__(
        self,
        source: AsyncIterator[httpx2.ServerSentEvent],
        fail: NetworkFailure,
        seen: Callable[[httpx2.ServerSentEvent], None],
    ) -> None:
        """Iterate *source*, mapping failures through *fail* and handing each event to *seen*."""
        super().__init__(source, fail)
        self._seen = seen

    @override
    async def __anext__(self) -> httpx2.ServerSentEvent:
        """Return the next event, after reporting it."""
        event = await super().__anext__()
        self._seen(event)
        return event
