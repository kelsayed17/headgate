# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""A call's request body: one ``body=``, whose value says what it is (:data:`Body`).

* :class:`EncodedBody` is the write side of ``expect=``: a flavor's ``writer(Type)``, or :func:`~headgate.writer_for`
  on any codec, builds a :data:`Writer`, a function from a value of that type to the ``EncodedBody`` the call sends, its
  bytes and their ``Content-Type``. A hand-written writer is a plain function returning an ``EncodedBody``.
* :class:`Json` is builtins sent as JSON, :class:`Form` a form, :class:`Multipart` files with form fields, and
  :class:`Raw` bytes or a stream of them.

A body is either replayable or not, and the retry layer has to know which (:data:`~headgate.retry.REPLAYABLE_BODY`):
a stream given as an iterable is consumed by the first send, so the call is never re-sent; a stream given as a factory
is built afresh, by calling the factory once, each time the request is sent. A multipart upload is replayable unless one
of its files is a file object that cannot be rewound, such as a pipe. :attr:`Raw.replayable` and
:attr:`Multipart.replayable` say which; every other body is bytes in memory.
"""

from __future__ import annotations

import json
import os
import stat
from collections.abc import AsyncGenerator, AsyncIterable, AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import IO, Final, final, override

import httpx2

from headgate._core import check_kind

__all__ = [
    "Body",
    "BodyFactory",
    "EncodedBody",
    "Form",
    "Json",
    "Lowered",
    "Multipart",
    "Raw",
    "RawContent",
    "UploadFiles",
    "Writer",
    "files_replayable",
    "lowered",
]


@dataclass(frozen=True, slots=True)
class EncodedBody:
    """A request body a :data:`Writer` encoded: its bytes, and the ``Content-Type`` they are sent with.

    The call sends ``content_type`` unless its own ``headers=`` set one. The body is bytes, so the call stays
    retryable.
    """

    content: bytes
    content_type: str = "application/json"


type Writer[T] = Callable[[T], EncodedBody]
"""Encodes a value of type ``T`` into the :class:`EncodedBody` a call sends as ``body=``."""


type BodyFactory = Callable[[], AsyncIterable[bytes]]
"""Builds a fresh request body stream; called once for each send of the request."""

type RawContent = bytes | AsyncIterable[bytes] | BodyFactory
"""What :class:`Raw` sends: bytes, a one-shot stream, or a factory of streams (which keeps the call retryable)."""

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


def _as_content(content: RawContent) -> bytes | AsyncIterable[bytes]:
    """Return what httpx2 is handed as ``content=`` for *content*: a factory becomes a stream built on every send."""
    if isinstance(content, bytes | AsyncIterable):
        return content
    return _Replayed(content)


@final
@dataclass(frozen=True, slots=True)
class Json:
    """A JSON body of builtins, sent with ``Content-Type: application/json``; a codec's ``to_builtins`` builds one.

    It is encoded as httpx2 encodes JSON, so ``Json(None)`` sends ``null``, and ``NaN`` or an infinity raises
    ``ValueError``. The ``Content-Type`` yields to one the call's ``headers=`` (or its view's) set.
    """

    value: object


@final
@dataclass(frozen=True, slots=True)
class Form:
    """A form body, sent as ``application/x-www-form-urlencoded``: each field's value as httpx2 encodes one.

    :raises TypeError: for *fields* that are not a mapping.
    """

    fields: Mapping[str, object]

    def __post_init__(self) -> None:
        """Refuse fields that are not a mapping, such as a list of pairs."""
        check_kind("fields", self.fields, Mapping, "a mapping of field names to values")


@final
@dataclass(frozen=True, slots=True)
class Multipart:
    """A ``multipart/form-data`` body: *files* as httpx2 takes them, and the form *fields* sent beside them.

    A file is ``{"name": file}``, or a ``(filename, file, content_type)`` tuple; ``file`` is bytes, text or a binary
    file object. The body can be sent again (:attr:`replayable`) unless a file object cannot be rewound.

    :raises TypeError: for *fields* that are not a mapping.
    """

    files: UploadFiles
    fields: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Refuse fields that are not a mapping."""
        check_kind("fields", self.fields, Mapping, "a mapping of field names to values")

    @property
    def replayable(self) -> bool:
        """Whether a retry or a 307/308 redirect can send the body again: every file is bytes, text or rewindable."""
        return files_replayable(self.files)


@final
@dataclass(frozen=True, slots=True)
class Raw:
    """A raw body: bytes; an async iterable of bytes, streamed once; or a callable returning one, called for each send.

    A stream given as an iterable is consumed by the first send, so the call is never re-sent and a redirect that asks
    for the body again is not followed (:attr:`replayable` is ``False``). Bytes and a factory are sent whole each time.

    :raises TypeError: for content that is none of the three, such as ``str`` (encode it first), ``bytearray`` or a
        synchronous iterable, which would otherwise be called as a factory and fail inside httpx2. A value that is both
        callable and iterable counts as an iterable.
    """

    content: RawContent

    def __post_init__(self) -> None:
        """Refuse content that is not bytes, an async iterable of bytes or a callable."""
        if not (isinstance(self.content, bytes | AsyncIterable) or callable(self.content)):
            msg = (
                "Raw takes bytes, an async iterable of bytes, or a callable returning one "
                f"(got {type(self.content).__name__})"
            )
            raise TypeError(msg)

    @property
    def replayable(self) -> bool:
        """Whether a retry or a 307/308 redirect can send the body again: bytes or a factory, not a one-shot stream."""
        return isinstance(self.content, bytes) or not isinstance(self.content, AsyncIterable)


type Body = EncodedBody | Json | Form | Multipart | Raw
"""A request body: what a writer encoded, JSON of builtins, a form, a multipart upload, or raw bytes or a stream."""

#: The classes a :data:`Body` is one of, for the kind check at the call.
_BODY_KINDS: Final = (EncodedBody, Json, Form, Multipart, Raw)


@dataclass(frozen=True, slots=True)
class Lowered:
    """A body as httpx2 is handed it, the ``Content-Type`` the call stamps, and whether it can be sent again."""

    content: bytes | AsyncIterable[bytes] | None = None
    content_type: str | None = None
    data: Mapping[str, object] | None = None
    files: UploadFiles | None = None
    replayable: bool = True


def lowered(body: Body | None) -> Lowered:
    """Return what httpx2 is handed for *body*, ``None`` being no body.

    :raises TypeError: for a value that is no :data:`Body`, such as bare ``bytes`` (write ``Raw(...)``) or a ``dict``
        (write ``Json(...)``).
    :raises ValueError: for a ``Json`` value holding ``NaN`` or an infinity, which JSON cannot carry.
    """
    if body is not None:
        check_kind("body", body, _BODY_KINDS, "a Body: Json, Form, Multipart, Raw or a writer's EncodedBody")
    match body:
        case EncodedBody(content=content, content_type=content_type):
            return Lowered(content, content_type)
        case Json(value=value):
            # As httpx2 encodes json=, so a body reads the same whichever spelling built it.
            encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
            return Lowered(encoded, "application/json")
        case Form(fields=fields):
            return Lowered(data=fields)
        case Multipart(files=files, fields=fields):
            return Lowered(data=fields, files=files, replayable=body.replayable)
        case Raw(content=content):
            return Lowered(_as_content(content), replayable=body.replayable)
        case _:
            return Lowered()


def _rewinds(file: IO[bytes]) -> bool:
    """Return whether *file* can be read again from its start: a regular file, or an in-memory one that can seek.

    A descriptor is judged by its type, not by ``seekable()``: on Windows a pipe reports itself seekable, though
    seeking it rewinds nothing.
    """
    try:
        descriptor = file.fileno()
    except OSError:  # io.UnsupportedOperation: no descriptor, such as a BytesIO
        return file.seekable()
    return stat.S_ISREG(os.fstat(descriptor).st_mode)


def files_replayable(files: UploadFiles) -> bool:
    """Return whether every file in *files* can be read again: bytes, text, a regular file or an in-memory one."""
    values = files.values() if isinstance(files, Mapping) else (value for _, value in files)
    contents = (value[1] if isinstance(value, tuple) else value for value in values)
    return all(isinstance(content, bytes | str) or _rewinds(content) for content in contents)
