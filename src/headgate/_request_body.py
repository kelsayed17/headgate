# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""A call's request body: raw ``content=`` (bytes, a stream, or a stream factory) and multipart ``files=``.

A streamed body is either replayable or not, and the retry layer has to know which: a stream given as an iterable is
consumed by the first send, so the call is marked non-replayable (:data:`~headgate.retry.REPLAYABLE_BODY`) and never
re-sent; a stream given as a factory is built afresh, by calling the factory once, each time the request is sent.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, AsyncIterable, AsyncIterator, Callable, Mapping, Sequence
from typing import IO, override

import httpx2

__all__ = ["Body", "BodyFactory", "UploadFiles", "check_one_body", "request_content"]

type BodyFactory = Callable[[], AsyncIterable[bytes]]
"""Builds a fresh request body stream; called once for each send of the request."""

type Body = bytes | AsyncIterable[bytes] | BodyFactory
"""A raw request body: bytes, a one-shot stream, or a factory of streams (which keeps the call retryable)."""

type _FileContent = IO[bytes] | bytes | str
type _UploadFile = (
    _FileContent
    | tuple[str | None, _FileContent]
    | tuple[str | None, _FileContent, str | None]
    | tuple[str | None, _FileContent, str | None, Mapping[str, str]]
)

type UploadFiles = Mapping[str, _UploadFile] | Sequence[tuple[str, _UploadFile]]
"""Multipart files, as httpx2 takes them: a file (or bytes), optionally with a filename, content type and headers."""


class _Replayed(httpx2.AsyncByteStream):
    """A request body built from its factory each time it is sent, so every attempt sends the whole body."""

    def __init__(self, factory: BodyFactory) -> None:
        """Build the body from *factory* on every send."""
        self._factory = factory

    @override
    async def __aiter__(self) -> AsyncIterator[bytes]:
        """Yield a fresh stream from the factory, closing it even when the send stops part-way."""
        chunks = aiter(self._factory())
        try:
            async for chunk in chunks:
                yield chunk
        finally:
            if isinstance(chunks, AsyncGenerator):
                await chunks.aclose()


def request_content(body: Body | None) -> tuple[bytes | AsyncIterable[bytes] | None, bool]:
    """Return what httpx2 is handed as ``content=`` for *body*, and whether the request can be sent again.

    Bytes and a factory are replayable; any other async iterable is consumed by the first send and is not. A value
    that is both callable and iterable counts as an iterable.
    """
    if body is None or isinstance(body, bytes):
        return body, True
    if isinstance(body, AsyncIterable):
        return body, False
    return _Replayed(body), True


def check_one_body(*, content: Body | None, json: object, data: object, files: UploadFiles | None) -> None:
    """Raise ``ValueError`` for body options httpx2 would silently drop one of.

    ``content=`` is the whole body, so it combines with nothing; ``files=`` is a multipart body, which takes form
    fields from ``data=`` but cannot carry ``json=``.
    """
    if content is not None and (json is not None or data is not None or files is not None):
        msg = "content= is the whole request body, so it cannot be combined with json=, data= or files="
        raise ValueError(msg)
    if files is not None and json is not None:
        msg = "files= sends a multipart body, which cannot carry json=; pass form fields as data="
        raise ValueError(msg)
