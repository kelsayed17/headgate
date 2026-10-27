# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""``headgate._responses.event_stream``, the stock server-sent events reader.

Parsing is delegated to httpx2's ``EventSource``, so these cases are written against the WHATWG grammar rather than
that implementation: they are what keeps swapping the parser a no-op. The client tests exercise the one-line
``event: message`` shape; these cover the rest of the grammar a server may send.
"""

from __future__ import annotations

import json

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
    """Skip a ``retry:`` hint or an ``event:``-only ping, which the grammar does not dispatch.

    httpx2's parser yields such a block with empty ``data``, which a JSON reader would then fail to decode.
    """
    body = b'retry: 3000\n\nevent: ping\n\nid: 7\ndata: {"id": 1}\n\n'
    assert _data(body) == ['{"id": 1}']


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
