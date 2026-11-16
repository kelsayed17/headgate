# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""``Client.events``: a server-sent event stream, opened with ``Accept: text/event-stream`` and no read limit.

It is :meth:`~headgate.Client.stream` with two defaults, so the ``Stream`` it opens is the one ``stream()`` opens, and
the caller's own ``headers=`` and ``timeout=`` win over either default.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

import httpx2
import pytest

from headgate import Client, HttpOptions, Timeouts, TransportPolicy

if TYPE_CHECKING:
    from headgate.streaming import StreamOptions

#: One event, as a server sends it.
_EVENT = b"id: 7\nevent: tick\ndata: hi\n\n"


class _Recording:
    """Answer every request with :data:`_EVENT`, recording the ``Accept`` it asked for and its read timeout."""

    def __init__(self) -> None:
        """Record nothing yet."""
        self.accepts: list[str | None] = []
        self.reads: list[object] = []
        self.last_event_ids: list[str | None] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        """Record *request* and answer it."""
        self.accepts.append(request.headers.get("accept"))
        self.reads.append(request.extensions["timeout"]["read"])
        self.last_event_ids.append(request.headers.get("last-event-id"))
        return httpx2.Response(200, headers={"Content-Type": "text/event-stream"}, content=_EVENT, request=request)


def _client(recording: _Recording) -> Client:
    """Return a client over *recording*, with no deadline to cut the timeouts it sees."""
    return Client(
        base_url="https://api.example.test",
        policy=TransportPolicy(deadline=None),
        http=HttpOptions(transport=httpx2.MockTransport(recording)),
    )


async def test_events_asks_for_an_event_stream_with_no_read_limit() -> None:
    """Send a GET with ``Accept: text/event-stream`` and no read timeout, and read its events from the stream."""
    recording = _Recording()
    async with _client(recording).events("feed") as stream:
        events = [event async for event in stream.aiter_events()]

    assert [(event.id, event.event, event.data) for event in events] == [("7", "tick", "hi")]
    assert (recording.accepts, recording.reads) == (["text/event-stream"], [None])


@pytest.mark.parametrize(
    ("options", "accept", "read"),
    [
        ({"headers": {"accept": "text/event-stream; charset=utf-8"}}, "text/event-stream; charset=utf-8", None),
        ({"timeout": Timeouts(read=10.0)}, "text/event-stream", 10.0),
        ({"timeout": Timeouts(connect=1.0)}, "text/event-stream", None),
    ],
    ids=["own-accept", "own-read", "other-phase"],
)
async def test_the_callers_accept_and_read_timeout_win(options: StreamOptions, accept: str, read: float | None) -> None:
    """Keep the caller's own ``Accept`` and read timeout over the defaults; a timeout without ``read`` sets none."""
    recording = _Recording()
    async with _client(recording).events("feed", **options):
        pass

    assert (recording.accepts, recording.reads) == ([accept], [read])


async def test_events_resumes_after_the_last_event_id() -> None:
    """Send ``Last-Event-ID``, as ``stream()`` does, to resume after the last event a previous stream saw."""
    recording = _Recording()
    async with _client(recording).events("feed", last_event_id="7"):
        pass

    assert recording.last_event_ids == ["7"]


def test_a_timeout_of_the_wrong_kind_is_refused_as_any_calls() -> None:
    """Refuse a ``timeout=`` that is not a ``Timeouts`` with the message any call gives, before anything is sent."""
    recording = _Recording()

    with pytest.raises(TypeError, match=r"^timeout= takes a Timeouts"):
        _client(recording).events("feed", timeout=cast("Any", 5.0))

    assert recording.reads == []
