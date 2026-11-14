# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""Request bodies: raw ``content=`` (bytes, a one-shot stream, a stream factory) and multipart ``files=``."""

from __future__ import annotations

import io
from typing import TYPE_CHECKING, Any, cast, override

import httpx2
import pytest

from headgate import Client, HttpOptions, Multipart, Rate, Raw, as_bytes, nothing
from headgate._core import CallContext
from headgate.retry._policy import REPLAYABLE_BODY

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

_CHUNKS = (b"alpha-", b"beta-", b"gamma")
_BODY = b"".join(_CHUNKS)


class _Server(httpx2.AsyncBaseTransport):
    """An in-memory network that reads every request body and answers 503 for the first *failures* requests.

    It iterates each body as a network transport does; ``httpx2.MockTransport`` reads it with ``aread()`` instead,
    which keeps it on the request for the next attempt.
    """

    def __init__(self, failures: int = 0) -> None:
        """Fail the first *failures* requests with 503."""
        self._failures = failures
        self.bodies: list[bytes] = []
        self.replayable: list[bool | None] = []
        self.content_types: list[str | None] = []

    @override
    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        """Record the request's body and replayability, then answer."""
        assert isinstance(request.stream, httpx2.AsyncByteStream)
        self.bodies.append(b"".join([chunk async for chunk in request.stream]))
        context = CallContext.find(request)
        self.replayable.append(None if context is None else context.get(REPLAYABLE_BODY))
        self.content_types.append(request.headers.get("content-type"))
        status = 503 if len(self.bodies) <= self._failures else 200
        return httpx2.Response(status, content=b"done", request=request)

    def client(self) -> Client:
        """Return a client sending to this server."""
        return Client(base_url="https://x.test", rate=Rate.per_second(100), http=HttpOptions(transport=self))


async def _chunks() -> AsyncIterator[bytes]:
    """Yield the test body in pieces."""
    for chunk in _CHUNKS:
        yield chunk


class _Iterable:
    """An async iterator over the test body that is not an async generator, so it has nothing to close."""

    def __init__(self) -> None:
        """Start at the first piece."""
        self._pieces = iter(_CHUNKS)

    def __aiter__(self) -> AsyncIterator[bytes]:
        """Return this iterator."""
        return self

    async def __anext__(self) -> bytes:
        """Return the next piece."""
        piece = next(self._pieces, None)
        if piece is None:
            raise StopAsyncIteration
        return piece


async def test_bytes_content_is_sent_and_stays_replayable() -> None:
    """Send a ``bytes`` body as it is, leaving the call replayable."""
    server = _Server()
    assert await server.client().post("upload", expect=as_bytes, body=Raw(_BODY)) == b"done"
    assert server.bodies == [_BODY]
    assert server.replayable == [None]


async def test_a_streamed_body_is_sent_once_and_marks_the_call_not_replayable() -> None:
    """Send an async iterable body and mark the call so the retry layer never re-sends it."""
    server = _Server()
    await server.client().put("upload", expect=nothing, body=Raw(_chunks()))
    assert server.bodies == [_BODY]
    assert server.replayable == [False]


async def test_a_body_factory_is_called_once_per_attempt() -> None:
    """Build the body afresh for each attempt, so a retried call sends the whole body every time."""
    server = _Server(failures=1)
    calls: list[int] = []

    def factory() -> AsyncIterator[bytes]:
        calls.append(1)
        return _chunks()

    await server.client().put("upload", expect=nothing, body=Raw(factory))
    assert server.bodies == [_BODY, _BODY]
    assert len(calls) == len(server.bodies)
    assert server.replayable == [None, None]


async def test_a_body_factory_may_return_any_async_iterable() -> None:
    """Accept a factory returning an async iterable that is not a generator."""
    server = _Server()
    await server.client().post("upload", expect=nothing, body=Raw(_Iterable))
    assert server.bodies == [_BODY]


async def test_a_streamed_upload_works_through_stream() -> None:
    """Send a streamed body on a streamed call."""
    server = _Server()
    async with server.client().stream("upload", method="POST", body=Raw(_chunks())) as stream:
        assert [chunk async for chunk in stream.aiter_bytes()] == [b"done"]
    assert server.bodies == [_BODY]


async def test_files_are_sent_as_multipart_with_form_fields() -> None:
    """Send ``files=`` as a multipart body carrying the ``data=`` fields beside the files."""
    server = _Server()
    files = {"report": ("report.csv", io.BytesIO(b"a,b\n1,2\n"), "text/csv")}
    await server.client().post("upload", expect=nothing, body=Multipart(files, {"kind": "monthly"}))
    (body,) = server.bodies
    (content_type,) = server.content_types
    assert content_type is not None
    assert content_type.startswith("multipart/form-data; boundary=")
    assert b'filename="report.csv"' in body
    assert b"a,b\n1,2\n" in body
    assert b'name="kind"' in body
    assert b"monthly" in body


async def test_a_multipart_upload_is_whole_on_every_attempt() -> None:
    """Re-read a file object from its start when a multipart call is retried.

    A POST that sent its body is re-sent only under an Idempotency-Key, so the call carries one.
    """
    server = _Server(failures=1)
    await server.client().post(
        "upload", expect=nothing, body=Multipart({"f": io.BytesIO(_BODY)}), idempotency_key="upload-1"
    )
    first, second = server.bodies
    assert _BODY in first
    assert _BODY in second


@pytest.mark.parametrize(
    ("content", "kind"),
    [("text", "str"), (bytearray(b"x"), "bytearray"), (memoryview(b"x"), "memoryview"), ([b"x"], "list")],
    ids=["str", "bytearray", "memoryview", "a-sync-iterable"],
)
def test_a_raw_content_that_is_no_body_is_refused_by_name(content: object, kind: str) -> None:
    """Refuse ``Raw`` content that is neither bytes, an async iterable nor a callable, naming it, when it is built."""
    with pytest.raises(TypeError) as raised:
        Raw(cast("Any", content))
    assert str(raised.value) == f"Raw takes bytes, an async iterable of bytes, or a callable returning one (got {kind})"
