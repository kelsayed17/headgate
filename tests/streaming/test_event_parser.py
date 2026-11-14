# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""``EventParser``, the incremental event-stream parser behind ``Stream.aiter_events`` and ``event_stream``.

The grammar itself is covered through those two readers; these cases are the parser's own: a body cut into chunks at
any point, and the exact bounds of ``max_event_size``.
"""

from __future__ import annotations

import pytest

from headgate.streaming._events import EventParser, EventTooLargeError

#: Every line ending the grammar knows, with a CR and a CRLF where a chunk boundary can split them from what follows.
_BODY = b": hi\r\ndata: a\r\ndata: b\r\rdata: c\n\nid: 1\r\ndata: d\r\n\r\n"


def _data(*chunks: bytes, max_event_size: int | None = None) -> list[str]:
    """Return the ``data`` of each event one parser reads from *chunks*, fed in order."""
    parser = EventParser(max_event_size)
    return [event.data for chunk in chunks for event in parser.feed(chunk)]


def test_a_body_cut_at_any_two_points_parses_as_it_does_whole() -> None:
    """Parse the same events however the body is cut into three chunks, an empty one included.

    A CR that ends one chunk may be the first half of a CRLF; the LF that may start the next is skipped once, and only
    then.
    """
    assert _data(_BODY) == ["a\nb", "c", "d"]
    for first in range(len(_BODY) + 1):
        for second in range(first, len(_BODY) + 1):
            chunks = (_BODY[:first], _BODY[first:second], _BODY[second:])
            assert _data(*chunks) == ["a\nb", "c", "d"], chunks


def test_only_a_byte_order_mark_that_starts_the_stream_is_skipped() -> None:
    """Skip a BOM at the very start, and leave one on a later line, which then names no field the grammar knows."""
    assert _data(b"data: a\n\n\xef\xbb\xbfdata: b\n\n") == ["a"]
    assert _data(b"\n\xef\xbb\xbfdata: b\n\n") == []


def test_an_event_without_an_event_field_is_a_message() -> None:
    """Name the very first event ``message`` when its block has no ``event`` field."""
    (event,) = EventParser().feed(b"data: a\n\n")
    assert event.event == "message"


def test_the_last_event_id_is_none_until_a_block_ends() -> None:
    """Report no last event ID while the first block is still open."""
    parser = EventParser()
    assert list(parser.feed(b"id: 1\ndata: a")) == []
    assert parser.last_event_id is None


def test_the_reconnection_time_persists_across_blocks() -> None:
    """Keep the last valid ``retry:`` on the parser from the moment its line ends, through blocks that do not name one.

    Each event's own ``retry`` stays the field its block named, as httpx2's does.
    """
    parser = EventParser()
    assert parser.retry is None
    assert list(parser.feed(b"retry: 5000\n")) == []
    assert (parser.retry, parser.last_event_id) == (5000, None)
    (event,) = parser.feed(b"\ndata: x\n\nretry: soon\n\n")
    assert (event.retry, parser.retry) == (None, 5000)
    (event,) = parser.feed(b"retry: 20\ndata: y\n\n")
    assert (event.retry, parser.retry) == (20, 20)


def test_events_of_exactly_the_size_limit_are_read() -> None:
    """Read an event whose field lines hold exactly ``max_event_size`` bytes, the first and every later one."""
    assert _data(b"data: abcd\n\ndata: efgh\n\n", max_event_size=10) == ["abcd", "efgh"]


def test_an_unended_line_of_exactly_the_size_limit_is_kept() -> None:
    """Keep a line not yet ended that holds exactly ``max_event_size`` bytes, and read its event once it ends."""
    assert _data(b"data: abcd", b"\n\n", max_event_size=10) == ["abcd"]


@pytest.mark.parametrize("body", [b"data: abcde\n\n", b"data: ab\ndata: cd\n\n"], ids=["one-line", "two-lines"])
def test_an_event_past_the_size_limit_is_refused(body: bytes) -> None:
    """Refuse an event past ``max_event_size``: one byte past it in one line, or past it only counting both lines."""
    with pytest.raises(EventTooLargeError):
        _data(body, max_event_size=10)


def test_comments_do_not_count_toward_the_size_limit() -> None:
    """Leave a comment line out of the event's size, however long it is."""
    assert _data(b": " + b"x" * 50 + b"\ndata: abcd\n\n", max_event_size=10) == ["abcd"]


#: A body whose limit a chunk boundary could tip: a BOM before the first line, and a comment far longer than the limit.
_EDGES = b"\xef\xbb\xbf: " + b"c" * 40 + b"\ndata: abcd\n\n: " + b"c" * 40 + b"\r\ndata: efgh\n\n"


def test_the_size_limit_holds_however_the_body_is_cut() -> None:
    """Read events of exactly ``max_event_size`` the same way however the body is cut into three chunks.

    Neither a comment, which is never part of an event, nor the byte order mark the first line drops counts while its
    line is still arriving.
    """
    assert _data(_EDGES, max_event_size=10) == ["abcd", "efgh"]
    for first in range(len(_EDGES) + 1):
        for second in range(first, len(_EDGES) + 1):
            chunks = (_EDGES[:first], _EDGES[first:second], _EDGES[second:])
            assert _data(*chunks, max_event_size=10) == ["abcd", "efgh"], chunks


def test_a_comment_that_never_ends_is_not_held() -> None:
    """Read on past a comment line that keeps arriving without end, holding none of it."""
    parser = EventParser(max_event_size=10)
    for chunk in (b": ", *(b"c" * 4096 for _ in range(64)), b"\ndata: abcd\n\n"):
        events = list(parser.feed(chunk))

    assert [event.data for event in events] == ["abcd"]


@pytest.mark.parametrize(
    ("value", "retry"),
    [
        pytest.param("0" * 5000 + "5", 5, id="leading-zeros-past-int-digit-limit"),
        pytest.param("9" * 400, 2**63 - 1, id="past-a-float"),
        pytest.param("9" * 5000, 2**63 - 1, id="past-int-digit-limit"),
        pytest.param(str(2**63 - 1), 2**63 - 1, id="the-largest-kept"),
    ],
)
def test_a_huge_reconnection_time_is_held_at_the_largest_one_kept(value: str, retry: int) -> None:
    """Read a ``retry:`` of any length of digits, holding one past 2**63 - 1 milliseconds at that."""
    parser = EventParser()
    (event,) = parser.feed(f"retry: {value}\ndata: x\n\n".encode())

    assert (event.retry, parser.retry) == (retry, retry)
