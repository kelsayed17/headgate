# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""``headgate._responses.event_stream``, the stock server-sent events reader.

It shares its parser with ``Stream.aiter_events``, and these cases are written against the WHATWG grammar rather than
that implementation: they are what keeps swapping the parser a no-op. The client tests exercise the one-line
``event: message`` shape and what arrives in chunks; these cover the rest of the grammar a server may send.
"""

from __future__ import annotations

import json

import pytest

from headgate._responses import event_stream
from tests.support.responses import response

_ONE_MEBIBYTE = 1024 * 1024


def _data(body: bytes) -> list[str]:
    """Return the ``data`` of each event :func:`event_stream` reads from *body*."""
    return [event.data for event in event_stream(response(body))]


def test_each_event_keeps_its_fields() -> None:
    """Return the events whole: their ``event``, ``data``, ``id`` and ``retry``, as a stream's events are."""
    (event,) = event_stream(response(b'event: update\nid: 7\nretry: 3000\ndata: {"id": 1}\n\n'))
    assert (event.event, event.data, event.id, event.retry) == ("update", '{"id": 1}', "7", 3000)


def test_each_event_yields_its_data() -> None:
    """Return the events in order."""
    body = b'event: message\ndata: {"id": 1}\n\nevent: message\ndata: {"id": 2}\n\n'
    assert _data(body) == ['{"id": 1}', '{"id": 2}']


def test_a_multi_line_data_field_is_joined_with_newlines() -> None:
    """Join the ``data`` lines of one event with a newline."""
    body = b'event: message\ndata: {"a":\ndata:  1}\n\n'
    assert _data(body) == ['{"a":\n 1}']


def test_crlf_line_endings_parse_like_lf() -> None:
    """Read CRLF line endings as LF ones."""
    body = b'event: message\r\ndata: {"id": 1}\r\n\r\n'
    assert _data(body) == ['{"id": 1}']


def test_comment_lines_are_ignored() -> None:
    """Skip comment lines, such as a keep-alive."""
    body = b': keep-alive\n\nevent: message\ndata: {"id": 1}\n\n'
    assert _data(body) == ['{"id": 1}']


def test_a_block_without_data_is_not_an_event() -> None:
    """Skip a ``retry:`` hint, an ``event:``-only ping or an ``id:`` alone, which the grammar does not dispatch.

    httpx2's parser yields such a block with empty ``data``, which a JSON reader would then fail to decode.
    """
    body = b'retry: 3000\n\nevent: ping\n\nid: 6\n\nid: 7\ndata: {"id": 1}\n\n'
    assert _data(body) == ['{"id": 1}']


def test_a_data_field_with_an_empty_value_is_an_event() -> None:
    """Dispatch a block whose ``data`` field is empty: its data buffer holds an empty line, so it is not empty."""
    assert _data(b"data\n\ndata:\n\n") == ["", ""]


def test_one_leading_byte_order_mark_is_skipped() -> None:
    """Skip one leading UTF-8 BOM, which would otherwise join the first field name and lose its event; not a second."""
    assert _data(b'\xef\xbb\xbfdata: {"id": 1}\n\n') == ['{"id": 1}']
    assert _data(b"\xef\xbb\xbf\xef\xbb\xbfdata: a\n\ndata: b\n\n") == ["b"]


def test_the_body_is_utf_8_whatever_the_charset() -> None:
    """Decode UTF-8, which the grammar requires of every event stream, replacing a malformed sequence."""
    body = "data: café\n\n".encode() + b"data: \xff\n\n"
    events = event_stream(response(body, headers={"content-type": "text/event-stream; charset=iso-8859-1"}))
    assert [event.data for event in events] == ["café", "�"]


@pytest.mark.parametrize("value", ["-5", "+5", " 5", "5.0", "1_000", "٣", ""])
def test_a_retry_that_is_not_ascii_digits_is_ignored(value: str) -> None:
    """Ignore a ``retry:`` field whose value is anything but ASCII digits."""
    (event,) = event_stream(response(f"retry: {value}\ndata: x\n\n".encode()))
    assert event.retry is None


def test_an_id_carries_over_to_later_events_until_the_server_resets_it() -> None:
    """Keep the last ``id`` for the events that name none, ignore one holding NUL, and reset it on an empty one."""
    body = b"id: 7\ndata: a\n\ndata: b\n\nid: 8\x009\ndata: c\n\nid\ndata: d\n\n"
    assert [event.id for event in event_stream(response(body))] == ["7", "7", "7", ""]


def test_cr_line_endings_parse_like_lf() -> None:
    """Read a bare CR as a line ending, as the grammar does."""
    assert _data(b"data: 1\rdata: 2\r\rdata: 3\r\r") == ["1\n2", "3"]


def test_an_event_name_lasts_one_event() -> None:
    """Name an event after its ``event`` field, and the next one ``message`` again."""
    events = event_stream(response(b"event: tick\ndata: 1\n\ndata: 2\n\n"))
    assert [event.event for event in events] == ["tick", "message"]


def test_an_unterminated_final_event_is_dropped() -> None:
    """Drop a final event that no blank line ends, as the grammar does at end of stream."""
    assert _data(b'data: {"id": 1}\n\ndata: {"id": 2}') == ['{"id": 1}']


def test_an_event_larger_than_one_mebibyte_is_read_whole() -> None:
    """Read a single event larger than httpx2's default 1 MiB limit.

    The body is already buffered and capped by the client, so no per-event limit applies here.
    """
    payload = json.dumps({"rows": ["x" * 64] * 40_000})
    assert len(payload) > _ONE_MEBIBYTE
    assert _data(f"event: message\ndata: {payload}\n\n".encode()) == [payload]
