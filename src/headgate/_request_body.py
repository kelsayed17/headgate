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

import enum
import json
import os
import stat
from collections.abc import AsyncGenerator, AsyncIterable, AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, time
from decimal import Decimal
from typing import IO, Final, NoReturn, final, override
from uuid import UUID

import httpx2

from headgate._core import check_kind

__all__ = [
    "Body",
    "EncodedBody",
    "Form",
    "Json",
    "Lowered",
    "Multipart",
    "Raw",
    "RawContent",
    "UploadFile",
    "UploadFiles",
    "WireValue",
    "Writer",
    "files_replayable",
    "lowered",
    "wire_fields",
    "wire_value",
]

type WireValue = str | int | float | bool | None
"""A query or form value httpx2 encodes itself."""

#: What a query or form value may be, for the refusal of one that is none of it.
_WIRE_VALUES: Final = (
    "a str, int, float, bool, None, an Enum of str or int, a UUID, Decimal, date, datetime or time for each value, or "
    "a list or tuple of them"
)


def wire_value(option: str, key: str, value: object, *, wanted: str = _WIRE_VALUES) -> WireValue:
    """Return what *value*, under *key* in *option*, means on the wire: the one allowlist of query and form values.

    A ``str``, number, ``bool`` or ``None`` is itself; an ``Enum`` its ``value``, which must be a ``str`` or an
    ``int``; a ``UUID`` or ``Decimal`` its text; a ``date``, ``datetime`` or ``time`` its ISO 8601 form. An allowlist,
    since any other value's ``str()`` is its Python ``repr``: a dataclass's ``Order(qty=1)``, a plain ``Enum``'s
    ``Status.OPEN``.

    :raises TypeError: for any other value, naming *option*, what it takes (*wanted*), *key* and the value's type.
    """
    if isinstance(value, enum.Enum):
        if isinstance(value.value, str | int):
            return value.value
    elif isinstance(value, str | int | float | bool | None):
        return value
    elif isinstance(value, UUID | Decimal):
        return str(value)
    elif isinstance(value, date | time):
        return value.isoformat()
    msg = (
        f"{option} takes {wanted} (got {type(value).__name__} under {key!r}): encode any other value into text first, "
        "such as with json.dumps"
    )
    raise TypeError(msg)


def wire_fields(option: str, fields: Mapping[str, object], *, raw: bool = False) -> dict[str, object]:
    """Return *fields* with each value through :func:`wire_value`, a list or tuple of them item by item.

    With *raw*, ``bytes`` is sent as it is too: a multipart part carries bytes, where a query or a form carries text.
    """
    wanted = f"bytes, {_WIRE_VALUES}" if raw else _WIRE_VALUES

    def encoded(key: str, value: object) -> object:
        return value if raw and isinstance(value, bytes) else wire_value(option, key, value, wanted=wanted)

    return {
        key: [encoded(key, item) for item in value] if isinstance(value, list | tuple) else encoded(key, value)
        for key, value in fields.items()
    }


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


type RawContent = bytes | AsyncIterable[bytes] | Callable[[], AsyncIterable[bytes]]
"""What :class:`Raw` sends: bytes, a one-shot stream, or a factory of streams (which keeps the call retryable).

A factory builds a fresh stream of the body, called once for each send of the request.
"""

type UploadFile = (
    IO[bytes]
    | bytes
    | str
    | tuple[str | None, IO[bytes] | bytes | str]
    | tuple[str | None, IO[bytes] | bytes | str, str | None]
    | tuple[str | None, IO[bytes] | bytes | str, str | None, Mapping[str, str]]
)
"""One multipart file, as httpx2 takes it: its content (a binary file, bytes or text), or a tuple of its filename (or
``None``) and content, then optionally its content type (or ``None``) and its headers."""


type UploadFiles = Mapping[str, UploadFile] | Sequence[tuple[str, UploadFile]]
"""Multipart files, as httpx2 takes them: each field name with its :data:`UploadFile`, as a mapping or as pairs."""


class _Replayed(httpx2.AsyncByteStream):
    """A request body built from its factory each time it is sent, so every attempt sends the whole body."""

    def __init__(self, factory: Callable[[], AsyncIterable[bytes]]) -> None:
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


#: What a :class:`Json` value may hold, and what to write for one that holds anything else.
_JSON_TAKES: Final = "Json takes builtins: dict, list, tuple, str, int, float, bool and None"
_JSON_INSTEAD: Final = (
    "write body=writer(Type)(value) with a flavor's writer, or body=Json(to_builtins(value)) with its to_builtins"
)


class _NotBuiltinsError(TypeError):
    """A :class:`Json` value held something JSON's encoder does not know: raised as it is."""


def _not_builtins(value: object) -> NoReturn:
    """Refuse *value*, which ``json.dumps`` met inside a :class:`Json` value and cannot encode, naming its type."""
    msg = f"{_JSON_TAKES} (got {type(value).__name__}): {_JSON_INSTEAD}"
    raise _NotBuiltinsError(msg)


@final
@dataclass(frozen=True, slots=True)
class Json:
    """A JSON body of builtins, sent with ``Content-Type: application/json``; a codec's ``to_builtins`` builds one.

    It is encoded where it is built, so a value JSON cannot carry fails there, and as httpx2 encodes JSON:
    ``Json(None)`` sends ``null``, a tuple an array and an ``int`` key a string. The ``Content-Type`` yields to one the
    call's ``headers=`` (or its view's) set.

    :raises TypeError: for a value that is not builtins (a struct, a dataclass, a model, a ``date``, a ``set``), naming
        its type: a flavor's ``writer(Type)`` encodes a typed value, and its ``to_builtins`` turns one into builtins.
    :raises ValueError: for ``NaN`` or an infinity, which JSON cannot carry.
    """

    value: object

    def __post_init__(self) -> None:
        """Encode the value once, refusing one that is not builtins or holds a number JSON cannot carry."""
        _encoded_json(self.value)


def _encoded_json(value: object) -> bytes:
    """Return *value* encoded as httpx2 encodes ``json=``, so a body reads the same whichever spelling built it.

    The encoding is not kept on the :class:`Json`, whose one field is its value: a call encodes it again, as it is sent.

    :raises TypeError: for a value that is not builtins, naming its type, or a key JSON cannot write.
    :raises ValueError: for ``NaN`` or an infinity.
    """
    try:
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False, default=_not_builtins)
    except TypeError as exc:
        if isinstance(exc, _NotBuiltinsError):
            raise
        # A key json.dumps cannot write, such as a tuple, which it refuses before asking default=.
        msg = f"{_JSON_TAKES} ({exc}): {_JSON_INSTEAD}"
        raise TypeError(msg) from None
    return encoded.encode()


@final
@dataclass(frozen=True, slots=True)
class Form:
    """A form body, sent as ``application/x-www-form-urlencoded``: each field's value as a query value is sent.

    A value is a ``str``, number, ``bool`` or ``None``, an ``Enum`` (sent as its value), a ``UUID`` or ``Decimal`` (its
    text), a ``date``, ``datetime`` or ``time`` (ISO 8601), or a list or tuple of them, which repeats the field. The
    fields are kept encoded.

    :raises TypeError: for *fields* that are not a mapping, and for a value that is none of those.
    """

    fields: Mapping[str, object]

    def __post_init__(self) -> None:
        """Refuse fields that are not a mapping, such as a list of pairs, or a value with no wire meaning."""
        check_kind("fields", self.fields, Mapping, "a mapping of field names to values")
        object.__setattr__(self, "fields", wire_fields("Form(fields=)", self.fields))


@final
@dataclass(frozen=True, slots=True)
class Multipart:
    """A ``multipart/form-data`` body: *files* as httpx2 takes them, and the form *fields* sent beside them.

    A file is ``{"name": file}``, or a ``(filename, file, content_type)`` tuple; ``file`` is bytes, text or a binary
    file object. The body can be sent again (:attr:`replayable`) unless a file object cannot be rewound. The *fields*
    take the values a :class:`Form` does, and ``bytes`` too, sent as they are; they are kept encoded.

    :raises TypeError: for *fields* that are not a mapping, and for a value with no wire meaning.
    :raises ValueError: for no files, which httpx2 would send as a form, not as ``multipart/form-data``: send fields
        alone as :class:`Form`.
    """

    files: UploadFiles
    fields: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Refuse fields that are not a mapping or hold a value with no wire meaning, and a body with no files."""
        check_kind("fields", self.fields, Mapping, "a mapping of field names to values")
        object.__setattr__(self, "fields", wire_fields("Multipart(fields=)", self.fields, raw=True))
        if not self.files:
            msg = "Multipart needs at least one file; send form fields alone as Form(fields)"
            raise ValueError(msg)

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
    """
    if body is not None:
        check_kind("body", body, _BODY_KINDS, "a Body: Json, Form, Multipart, Raw or a writer's EncodedBody")
    match body:
        case EncodedBody(content=content, content_type=content_type):
            return Lowered(content, content_type)
        case Json(value=value):
            return Lowered(_encoded_json(value), "application/json")
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
