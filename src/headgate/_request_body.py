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
import io
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

from headgate._core import check_kind, is_model_class, named, refuse_subclass, sealed

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
    "encoded_json",
    "files_replayable",
    "lowered",
    "query_values",
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


def query_values(params: Mapping[str, object]) -> Mapping[str, object]:
    """Return *params* with every value of a repeated key: an ``httpx2.QueryParams`` as a list per repeated key.

    A ``QueryParams`` is a mapping whose items give each key's first value only, so ``a=1&a=2`` would lose ``2``.
    """
    if not isinstance(params, httpx2.QueryParams):
        return params
    return {key: values if len(values := params.get_list(key)) > 1 else values[0] for key in params}


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


@final
@sealed("build one of the encoded bytes and their content type, as a flavor's writer does")
@dataclass(frozen=True, slots=True)
class EncodedBody:
    """A request body a :data:`Writer` encoded: its bytes, and the ``Content-Type`` they are sent with.

    The call sends ``content_type`` unless its own ``headers=`` set one. The body is bytes, so the call stays
    retryable.

    :raises TypeError: for *content* that is not ``bytes``, and a *content_type* that is not a ``str``.
    """

    content: bytes
    content_type: str = "application/json"

    def __post_init__(self) -> None:
        """Refuse content that is not bytes, and a content type that is not a str."""
        check_kind("content", self.content, bytes, "the encoded body, bytes")
        check_kind("content_type", self.content_type, str, "a media type, such as 'application/json'")


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


#: What a ``Json`` says of a change to it, which names no slot: what it holds is its value, encoded when it was built.
_IMMUTABLE: Final = "Json is immutable: build a new one, or copy.replace(body, value=...)"


@final
class Json:
    """A JSON body of builtins, sent with ``Content-Type: application/json``; a codec's ``to_builtins`` builds one.

    It is encoded once, where it is built, as httpx2 encodes JSON: ``Json(None)`` sends ``null``, a tuple an array and
    an ``int`` key a string. So a value JSON cannot carry fails there, and a change to the value afterwards changes
    nothing that is sent. The ``Content-Type`` yields to one the call's ``headers=`` (or its view's) set. A ``Json`` is
    a value of its ``value``: equal, hashed (when the value hashes), shown and matched (``case Json(value)``) by it,
    copied with ``copy.replace(body, value=...)``, and copied, deep-copied and pickled as a ``Json`` built afresh from
    it.

    :raises TypeError: for a value that is not builtins (a struct, a dataclass, a model, a ``date``, a ``set``), naming
        its type: a flavor's ``writer(Type)`` encodes a typed value, and its ``to_builtins`` turns one into builtins.
    :raises ValueError: for ``NaN`` or an infinity, which JSON cannot carry, a value that holds itself, and text
        holding a lone surrogate, which UTF-8 cannot encode, naming it.
    """

    __slots__ = ("_encoded", "_value")
    __match_args__ = ("value",)

    _value: object
    _encoded: bytes

    def __init__(self, value: object) -> None:
        """Encode *value*, refusing one that is not builtins or holds what JSON cannot carry."""
        object.__setattr__(self, "_value", value)
        object.__setattr__(self, "_encoded", encoded_json(value))

    @property
    def value(self) -> object:
        """The builtins the body was built from, as given; what is sent is their encoding when it was built."""
        return self._value

    @override
    def __setattr__(self, name: str, value: object) -> None:
        """Refuse every change: the body was encoded when it was built."""
        raise AttributeError(_IMMUTABLE)

    @override
    def __delattr__(self, name: str) -> None:
        """Refuse every change: the body was encoded when it was built."""
        raise AttributeError(_IMMUTABLE)

    def __replace__(self, *, value: object) -> Json:
        """Return a ``Json`` of *value*, for ``copy.replace``."""
        return Json(value)

    @override
    def __reduce__(self) -> tuple[type[Json], tuple[object]]:
        """Rebuild this body from its value, for ``pickle`` and ``copy``, which would set its slots past the refusal."""
        return Json, (self._value,)

    @override
    def __eq__(self, other: object) -> bool:
        return self.value == other.value if isinstance(other, Json) else NotImplemented

    @override
    def __hash__(self) -> int:
        return hash(self.value)

    @override
    def __repr__(self) -> str:
        return f"Json(value={self.value!r})"

    def __init_subclass__(cls, **kwargs: object) -> None:
        """Refuse a subclass: the body is its encoded value, which nothing overriding a method would change."""
        del kwargs
        refuse_subclass(cls, Json, "build a Json of the value, or write a body with a flavor's writer")


def _is_mapping(value: object) -> bool:
    """Return whether *value* is a mapping JSON does not encode as one, since it is not a ``dict``."""
    return isinstance(value, Mapping) and not isinstance(value, dict)


def encoded_json(
    value: object,
    *,
    subject: str = "Json",
    takes: str = _JSON_TAKES,
    instead: str = _JSON_INSTEAD,
    compact: bool = True,
) -> bytes:
    """Return *value* encoded as httpx2 encodes ``json=``, so a body reads the same whichever spelling built it.

    Not *compact*, it is spelt as ``json.dumps`` spells it by default instead: a space after each separator, and the
    text escaped to ASCII. A refusal is worded for where the value was given: *subject* is what takes it, *takes* what
    it takes and *instead* what to write instead, a ``Json``'s by default.

    :raises TypeError: for a value that is not builtins, naming its type, or a key JSON cannot write.
    :raises ValueError: for ``NaN`` or an infinity, a value that holds itself, or text holding a lone surrogate, which
        UTF-8 cannot encode, naming the surrogate.
    """
    refused: list[object] = []

    def not_builtins(item: object) -> NoReturn:
        refused.append(item)
        raise TypeError(item)

    try:
        encoded = json.dumps(
            value,
            ensure_ascii=not compact,
            separators=(",", ":") if compact else None,
            allow_nan=False,
            default=not_builtins,
        )
    except TypeError as exc:
        # A plain TypeError at the boundary: neither the encoder's error nor not_builtins' adds anything to it.
        got = f"got {type(refused[0]).__name__}" if refused else str(exc)
        # A mapping that is not a dict (a MappingProxyType, say) is a dict away from JSON: copy it.
        mapping = bool(refused) and _is_mapping(refused[0])
        advice = "write dict(value) for a mapping that is not a dict" if mapping else instead
        msg = f"{takes} ({got}): {advice}"
        raise TypeError(msg) from None
    except ValueError as exc:
        msg = (
            f"{subject} takes a value JSON can carry ({exc}): write None or a string in place of NaN and the "
            "infinities, and break a value that holds itself"
        )
        raise ValueError(msg) from None
    try:
        return encoded.encode()
    except UnicodeEncodeError as exc:
        # Only the surrogate is named: the text around it is the body's, which may carry a credential.
        surrogate = exc.object[exc.start : exc.end]
        msg = (
            f"{subject} takes a value JSON can carry (got the lone surrogate {surrogate!r}, which UTF-8 cannot "
            "encode): decode the text it came from with errors='replace', or drop the surrogate"
        )
        raise ValueError(msg) from None


@final
@sealed("build a Form of the fields")
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


#: How many items a multipart file given as a pair holds: its field name, and the file.
_PAIR: Final = 2

#: The most items a file's tuple holds: its filename, the file, its content type and its headers.
_WITH_HEADERS: Final = 4

#: What multipart files are, and each file, for the refusal of anything else.
_UPLOAD_FILES: Final = "a mapping of field names to files, or a sequence of (name, file) pairs"
_UPLOAD_FILE: Final = (
    "a file for each field: bytes, text or a binary file object, alone or as (filename, file), "
    "(filename, file, content_type) or (filename, file, content_type, headers)"
)


def _is_content(value: object) -> bool:
    """Return whether *value* is what a part sends as httpx2 reads it: bytes, text, or a file object not in text mode.

    A file object is told by its ``read``, as httpx2 reads it, so one that is no ``io.IOBase`` (a named temporary
    file) is one too.
    """
    return isinstance(value, bytes | str) or (
        callable(getattr(value, "read", None)) and not isinstance(value, io.TextIOBase)
    )


def _is_upload_file(file: object) -> bool:
    """Return whether *file* is one of an :data:`UploadFile`'s shapes: content alone, or in a tuple of two to four."""
    if not isinstance(file, tuple):
        return _is_content(file)
    if not _PAIR <= len(file) <= _WITH_HEADERS:
        return False
    filename, content, *rest = file
    content_type = rest[0] if rest else None
    headers = rest[1] if len(file) == _WITH_HEADERS else {}
    return (
        isinstance(filename, str | None)
        and _is_content(content)
        and isinstance(content_type, str | None)
        and isinstance(headers, Mapping)
        and all(isinstance(name, str) and isinstance(value, str) for name, value in headers.items())
    )


def _shape(file: object) -> str:
    """Return what *file* is, for a refusal: a tuple by the types of its items, which keeps a file's bytes out of it."""
    if isinstance(file, tuple):
        return f"a tuple of {', '.join(type(item).__name__ for item in file) or 'nothing'}"
    return named(file)


@final
@sealed("build a Multipart of the files and fields")
@dataclass(frozen=True, slots=True)
class Multipart:
    """A ``multipart/form-data`` body: *files* as httpx2 takes them, and the form *fields* sent beside them.

    A file is ``{"name": file}``, or a ``(filename, file, content_type)`` tuple; ``file`` is bytes, text or a binary
    file object, anything with a ``read``. The body can be sent again (:attr:`replayable`) unless a file object cannot
    be rewound: a pipe, or one with only its ``read``. The *fields*
    take the values a :class:`Form` does, and ``bytes`` too, sent as they are; they are kept encoded.

    :raises TypeError: for *files* that are neither a mapping nor a sequence of pairs, a field name that is not a
        ``str``, a file of none of an :data:`UploadFile`'s shapes (naming its field), *fields* that are not a mapping,
        and for a value with no wire meaning.
    :raises ValueError: for no files, which httpx2 would send as a form, not as ``multipart/form-data``: send fields
        alone as :class:`Form`.
    """

    files: UploadFiles
    fields: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Refuse files and fields of the wrong kind, a field with no wire meaning, and a body with no files."""
        pairs = isinstance(self.files, Sequence) and all(
            isinstance(pair, tuple) and len(pair) == _PAIR for pair in self.files
        )
        if not (isinstance(self.files, Mapping) or pairs):
            check_kind("files", self.files, (), _UPLOAD_FILES)
        for name, file in self.files.items() if isinstance(self.files, Mapping) else self.files:
            if not isinstance(name, str):
                msg = f"files= takes {_UPLOAD_FILES} (got the key {name!r}, of type {type(name).__name__})"
                raise TypeError(msg)
            if not _is_upload_file(file):
                msg = f"files= takes {_UPLOAD_FILE} (got {_shape(file)} under {name!r})"
                raise TypeError(msg)
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
@sealed("build a Raw of the content and its content type")
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

    :raises TypeError: for a value that is no :data:`Body`, naming the body that holds it: ``Raw`` for bare
        ``bytes``, a stream or a file's bytes, ``Json`` for a mapping, and a writer of its type for a model's instance.
    """
    if body is not None and not isinstance(body, _BODY_KINDS):
        msg = f"body= takes a Body: Json, Form, Multipart, Raw or a writer's EncodedBody (got {named(body)})"
        hint = _body_hint(body)
        raise TypeError(msg if hint is None else f"{msg}: {hint}")
    match body:
        case EncodedBody(content=content, content_type=content_type):
            return Lowered(content, content_type)
        case Json(_encoded=encoded):
            return Lowered(encoded, "application/json")
        case Form(fields=fields):
            return Lowered(data=fields)
        case Multipart(files=files, fields=fields):
            return Lowered(data=fields, files=files, replayable=body.replayable)
        case Raw(content=content):
            return Lowered(_as_content(content), replayable=body.replayable)
        case _:
            return Lowered()


def _body_hint(body: object) -> str | None:
    """Return the body to write *body* as, a value that is no :data:`Body`, or ``None`` when no body holds it.

    ``Raw`` for bytes, an async stream or a file's bytes, ``Json`` for a mapping, and a writer of its type for a model's
    instance (a dataclass's, a ``NamedTuple``'s, a msgspec ``Struct``'s, a pydantic model's, an attrs class's).
    """
    if isinstance(body, bytes | AsyncIterable):
        return "write body=Raw(value)"
    if isinstance(body, Mapping):
        return "write body=Json(value)" if isinstance(body, dict) else "write body=Json(dict(value))"
    if isinstance(body, type):
        return None  # a class, a model's included, is no value to send
    if is_model_class(type(body)):
        name = type(body).__name__
        return (
            f"write body=writer({name})(value) with a flavor's writer, or body=Json(to_builtins(value)) with its "
            "to_builtins"
        )
    if callable(getattr(body, "read", None)):
        return "write body=Raw(value.read())"
    return None


def _rewinds(file: IO[bytes]) -> bool:
    """Return whether *file* can be read again from its start: a regular file, or an in-memory one that can seek.

    A descriptor is judged by its type, not by ``seekable()``: on Windows a pipe reports itself seekable, though
    seeking it rewinds nothing. A file object is told by its ``read`` alone, as httpx2 reads one, so one without a
    descriptor rewinds only when it says it can seek and has the ``seek`` httpx2 rewinds it with: one with only its
    ``read``, which httpx2 sends in chunks, cannot be rewound.
    """
    try:
        descriptor = file.fileno()
    except AttributeError, OSError:  # no descriptor: a BytesIO's io.UnsupportedOperation, or no fileno at all
        seekable = getattr(file, "seekable", None)
        return callable(seekable) and bool(seekable()) and callable(getattr(file, "seek", None))
    return stat.S_ISREG(os.fstat(descriptor).st_mode)


def files_replayable(files: UploadFiles) -> bool:
    """Return whether every file in *files* can be read again: bytes, text, a regular file or an in-memory one."""
    values = files.values() if isinstance(files, Mapping) else (value for _, value in files)
    contents = (value[1] if isinstance(value, tuple) else value for value in values)
    return all(isinstance(content, bytes | str) or _rewinds(content) for content in contents)
