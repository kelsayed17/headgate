# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""A body that cannot be sent twice is never sent twice, by any layer; a hedge copies only an in-memory body.

The layer below the retry layer may read a request's body again on its own: httpcore2's pool moves a request a GOAWAY
proved unprocessed to a new connection and sends it there. A one-shot body refuses that second pass at the stream
itself, rather than going out empty. A key that is empty or blank is no idempotency key.
"""

from __future__ import annotations

import asyncio
import io
import os
from typing import TYPE_CHECKING, BinaryIO, ClassVar, override

import httpx2
import pytest

from headgate import HttpOptions, IdempotencyKeys, RetryPolicy, TransportPolicy, UpstreamStatusError, nothing
from headgate.errors import TransportError
from headgate.retry import Hedge
from headgate.testing import FakeTransport, TestClock
from tests.integration._support import client

if TYPE_CHECKING:
    from collections.abc import AsyncIterable, AsyncIterator, Callable, Iterator

    from headgate._middleware import Next

_CHUNKS = (b"first,", b"second")
_BODY = b"".join(_CHUNKS)

#: How long a slow first send takes, on the test clock: longer than the hedge's ``after``.
_SLOW = 1.0
_HEDGE = Hedge(after=0.5)


class _AsyncFile:
    """An async-iterable file, such as aiofiles opens: not a generator, and empty once read to its end."""

    def __init__(self) -> None:
        """Hold the body to read once."""
        self._chunks = list(_CHUNKS)

    def __aiter__(self) -> AsyncIterator[bytes]:
        """Return an iterator over what is left of the file."""
        return self._read()

    async def _read(self) -> AsyncIterator[bytes]:
        while self._chunks:
            yield self._chunks.pop(0)


async def _generated() -> AsyncIterator[bytes]:
    """Yield the body from an async generator, which httpx2 refuses to iterate twice."""
    for chunk in _CHUNKS:
        yield chunk


class _MovesToANewConnection(httpx2.AsyncBaseTransport):
    """Read each request's body twice, as httpcore2's pool does when a GOAWAY moves the request to a new connection."""

    def __init__(self) -> None:
        """Record what the second connection received."""
        self.processed: list[bytes] = []

    @override
    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        stream = request.stream
        assert isinstance(stream, httpx2.AsyncByteStream)
        _ = [chunk async for chunk in stream]  # the first connection: GOAWAY, never processed
        self.processed.append(b"".join([chunk async for chunk in stream]))
        return httpx2.Response(201)


@pytest.mark.parametrize("body", [_AsyncFile, _generated], ids=["async-file", "async-generator"])
async def test_a_one_shot_body_refuses_the_pools_second_pass(body: Callable[[], AsyncIterable[bytes]]) -> None:
    """Raise ``TransportError`` when the pool reads a one-shot body again, rather than sending it empty."""
    network = _MovesToANewConnection()
    built = client(network)

    with pytest.raises(TransportError, match="one-shot"):
        await built.post("upload", content=body(), expect=nothing)

    assert network.processed == []


@pytest.mark.parametrize("body", [_BODY, _generated], ids=["bytes", "factory"])
async def test_a_replayable_body_is_sent_whole_on_the_pools_second_pass(
    body: bytes | Callable[[], AsyncIterator[bytes]],
) -> None:
    """Let the pool re-send bytes or a factory's body: each pass sends all of it."""
    network = _MovesToANewConnection()
    built = client(network)

    await built.post("upload", content=body, expect=nothing)

    assert network.processed == [_BODY]


@pytest.fixture
def pipe() -> Iterator[BinaryIO]:
    """Yield the read end of a pipe holding the body: a file that can be neither rewound nor sized."""
    read_fd, write_fd = os.pipe()
    os.write(write_fd, _BODY)
    os.close(write_fd)
    with os.fdopen(read_fd, "rb") as file:
        yield file


async def test_an_upload_of_a_file_that_cannot_rewind_is_not_sent_again(pipe: BinaryIO) -> None:
    """Send a ``files=`` upload of a pipe once, though its 502 is retryable, rather than again with an empty file."""
    fake = FakeTransport().reply("POST", "/v1/upload", 502, 201)
    built = client(fake)

    with pytest.raises(UpstreamStatusError) as raised:
        await built.post("upload", files={"file": ("data.bin", pipe)}, idempotency_key="k", expect=nothing)

    assert raised.value.status_code == httpx2.codes.BAD_GATEWAY
    assert len(fake.calls) == 1
    assert "the request body is a one-shot stream, so it is not sent again" in raised.value.__notes__


async def test_an_upload_of_a_file_that_can_rewind_is_sent_whole_again() -> None:
    """Retry a ``files=`` upload of a ``BytesIO``: it rewinds, so the retry sends the whole file."""
    fake = FakeTransport().reply("POST", "/v1/upload", 502, 201)
    built = client(fake)

    await built.post("upload", files={"file": ("data.bin", io.BytesIO(_BODY))}, idempotency_key="k", expect=nothing)

    assert len(fake.calls) == len(("failed", "retried"))
    assert all(_BODY in request.content for request in fake.calls)


async def test_a_hedge_copies_an_in_memory_body() -> None:
    """Hedge a slow PUT of bytes with a copy of the same bytes."""
    clock = TestClock()

    async def slow(_request: httpx2.Request) -> httpx2.Response:
        await clock.sleep(_SLOW)
        return httpx2.Response(200)

    fake = FakeTransport().reply("PUT", "/v1/x", slow, 200)
    with clock.install():
        built = client(fake, policy=TransportPolicy(retry=RetryPolicy(hedge=_HEDGE, budget=None)))

    await built.put("x", content=_BODY, expect=nothing)

    assert [request.content for request in fake.calls] == [_BODY, _BODY]


async def test_a_hedge_never_shares_a_streamed_body() -> None:
    """Send a slow multipart PUT alone: a hedge would read the same file from two sends at once."""
    clock = TestClock()

    async def slow(_request: httpx2.Request) -> httpx2.Response:
        await clock.sleep(_SLOW)
        return httpx2.Response(200)

    fake = FakeTransport().reply("PUT", "/v1/x", slow, 200)
    with clock.install():
        built = client(fake, policy=TransportPolicy(retry=RetryPolicy(hedge=_HEDGE, budget=None)))

    await built.put("x", files={"file": ("data.bin", io.BytesIO(_BODY))}, expect=nothing)

    assert len(fake.calls) == 1


class _ReadsContent:
    """A middleware that reads the request's body, as ``docs/configuration.md`` says one may."""

    credential_headers: ClassVar = frozenset[str]()
    credential_params: ClassVar = frozenset[str]()

    def __init__(self) -> None:
        """Record the bodies read."""
        self.read: list[bytes] = []

    async def __call__(self, request: httpx2.Request, call_next: Next) -> httpx2.Response:
        """Read ``request.content``, then pass the request on."""
        self.read.append(request.content)
        return await call_next(request)


async def test_a_hedge_can_be_read_by_middleware() -> None:
    """Let a middleware read a hedge's ``request.content``, so the hedge answers instead of the call failing."""
    clock = TestClock()

    async def hangs(_request: httpx2.Request) -> httpx2.Response:
        await asyncio.Event().wait()
        raise AssertionError  # pragma: no cover - cancelled once the hedge answers

    middleware = _ReadsContent()
    fake = FakeTransport().reply("GET", "/v1/x", hangs, 200)
    with clock.install():
        built = client(
            fake,
            policy=TransportPolicy(retry=RetryPolicy(hedge=_HEDGE, budget=None)),
            http=HttpOptions(middleware=(middleware,)),
        )

    await built.get("x", expect=nothing)

    assert middleware.read == [b"", b""]


@pytest.mark.parametrize("key", ["", "  "], ids=["empty", "blank"])
async def test_an_empty_idempotency_header_does_not_make_a_post_retryable(key: str) -> None:
    """Send a POST whose ``Idempotency-Key`` header is empty or blank once: it carries no key."""
    fake = FakeTransport().reply("POST", "/v1/x", 503, 201)
    built = client(fake)

    with pytest.raises(UpstreamStatusError):
        await built.post("x", content=_BODY, headers={"Idempotency-Key": key}, expect=nothing)

    assert len(fake.calls) == 1


@pytest.mark.parametrize("key", ["", "  "], ids=["empty", "blank"])
async def test_an_empty_idempotency_key_is_refused(key: str) -> None:
    """Refuse an ``idempotency_key=`` that is empty or blank, before anything is sent."""
    fake = FakeTransport().reply("POST", "/v1/x", 201)
    built = client(fake)

    with pytest.raises(ValueError, match="idempotency key"):
        await built.post("x", content=_BODY, idempotency_key=key, expect=nothing)

    assert not fake.calls


async def test_an_empty_key_from_the_factory_is_refused() -> None:
    """Refuse a key the policy's ``IdempotencyKeys`` factory made empty, before anything is sent."""
    fake = FakeTransport().reply("POST", "/v1/x", 201)
    built = client(fake, policy=TransportPolicy(idempotency=IdempotencyKeys(factory=lambda: "")))

    with pytest.raises(ValueError, match="idempotency key"):
        await built.post("x", content=_BODY, expect=nothing)

    assert not fake.calls
