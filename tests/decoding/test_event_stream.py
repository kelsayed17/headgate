# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""``headgate._responses.event_stream``, the stock SSE reader, pinned on its own.

Parsing is delegated to httpx2's ``EventSource``, so these cases are written against the WHATWG grammar rather than
against that implementation: they are what keeps swapping the parser a no-op. The client tests exercise the one-line
``event: message`` shape; these cover the rest of the grammar a server may send.
"""

from __future__ import annotations

import json

from headgate._responses import event_stream
from tests.support.responses import response


def test_each_event_yields_its_data() -> None:
    body = b'event: message\ndata: {"id": 1}\n\nevent: message\ndata: {"id": 2}\n\n'
    assert event_stream(response(body)) == [b'{"id": 1}', b'{"id": 2}']


def test_a_multi_line_data_field_is_joined_with_newlines() -> None:
    body = b'event: message\ndata: {"a":\ndata:  1}\n\n'
    assert event_stream(response(body)) == [b'{"a":\n 1}']


def test_crlf_line_endings_parse_like_lf() -> None:
    body = b'event: message\r\ndata: {"id": 1}\r\n\r\n'
    assert event_stream(response(body)) == [b'{"id": 1}']


def test_comment_lines_are_ignored() -> None:
    body = b': keep-alive\n\nevent: message\ndata: {"id": 1}\n\n'
    assert event_stream(response(body)) == [b'{"id": 1}']


def test_an_event_larger_than_one_mebibyte_is_read_whole() -> None:
    """A large tool reply is one event. httpx2's ``EventSource`` refuses events over 1 MiB by default;
    the body is already buffered and capped by ``read_capped``, so no per-event limit applies here.
    """
    payload = json.dumps({"rows": ["x" * 64] * 40_000})  # ~2.7 MB
    assert len(payload) > 1024 * 1024
    assert event_stream(response(f"event: message\ndata: {payload}\n\n".encode())) == [payload.encode()]
