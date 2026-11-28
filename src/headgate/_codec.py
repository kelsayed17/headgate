# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""Codecs: what headgate needs of a serialization library, and the readers and writers built on one.

The core imports no serialization library. A :class:`Codec` is the one interface between them: how the library reads
bytes into a type expression and writes a value back, the media type of its bytes, which of its exceptions mean a body
of the wrong shape, and how to describe one without repeating the input. :func:`reader_for` and :func:`writer_for`
build a reader and a writer on any codec, and :func:`decode_with` decodes bytes that are not a whole response.

``headgate.msgspec`` and ``headgate.pydantic`` are two codecs. :func:`headgate.testing.check_codec` holds any codec to
the promises theirs keep: strict types, unknown fields ignored, malformed bytes refused, and messages that never quote
the input.
"""

from collections.abc import Callable
from typing import Final, Protocol, runtime_checkable

from typing_extensions import TypeForm

from headgate._core import check_kind, type_text
from headgate._failures import NESTED_TOO_DEEPLY, shorten
from headgate._request_body import EncodedBody, Writer
from headgate._responses import Reader, _body_reader
from headgate.errors import DecodeError, RefusedError

__all__ = ["Codec", "decode_with", "drop_none", "reader_for", "writer_for"]


@runtime_checkable
class Codec(Protocol):
    """A serialization library, as headgate uses one.

    * ``media_type``: what its bytes are (``"application/json"``, ``"application/msgpack"``): the ``Content-Type`` a
      writer sends.
    * ``errors``: the exceptions it raises for bytes that are malformed or do not match the type. A reader turns them
      into :class:`~headgate.errors.DecodeError`, which a call raises as ``ResponseParseError``, so a library's own
      validation error never escapes the ``ClientError`` tree.
    * ``decoder(type_)`` and ``encoder(type_)``: a function reading bytes into the type expression, and one writing a
      value of it back. Each is built once per type, so a library that compiles a type (a msgspec decoder, a pydantic
      adapter) does so once. ``decoder`` raises ``TypeError`` for a type the library cannot decode, so a reader of one
      fails where it is built rather than at its first body.

    A codec handles the built-in containers (``dict``, ``list`` and the scalars in them) besides its own record types:
    :func:`reader_for` encodes an empty ``list[object]`` for ``allow_empty_body=True``, and
    :func:`headgate.testing.check_codec` writes and reads ``dict[str, int]`` and ``dict[str, str]``.
    * ``describe(error)``: a message for one of ``errors`` that never quotes the input, since the body may carry a
      credential. The reader bounds its length.

    It is ``runtime_checkable``: ``isinstance(value, Codec)`` asks whether *value* has the five members, as every
    function here that takes a codec does, refusing anything else with ``TypeError``, and a codec's class too, which
    has them as functions: pass an instance.
    """

    @property
    def media_type(self) -> str:
        """The media type of the bytes this codec reads and writes."""
        ...

    @property
    def errors(self) -> tuple[type[Exception], ...]:
        """The exceptions this codec raises for malformed or mistyped bytes."""
        ...

    def decoder[T](self, type_: TypeForm[T], /) -> Callable[[bytes], T]:
        """Return a function that reads bytes into the type expression *type_*."""
        ...

    def encoder[T](self, type_: TypeForm[T], /) -> Callable[[T], bytes]:
        """Return a function that writes a value of the type expression *type_* as bytes."""
        ...

    def describe(self, error: Exception, /) -> str:
        """Return a message for *error*, one of :attr:`errors`, that does not quote the input."""
        ...


#: What a ``codec=`` takes, for the refusal of anything else.
_CODEC_WANTED: Final = "a Codec, such as headgate.msgspec.CODEC"


def check_is_codec(codec: object) -> None:
    """Refuse a *codec* that is not a :class:`Codec`, or is a codec's class, which has the members as functions.

    :raises TypeError: naming ``codec=``, and saying to pass an instance for a codec's class.
    """
    if isinstance(codec, type) and isinstance(codec, Codec):
        name = codec.__name__
        msg = f"codec= takes {_CODEC_WANTED} (got the class {name}): pass an instance, such as {name}()"
        raise TypeError(msg)
    check_kind("codec", codec, Codec, _CODEC_WANTED)


#: What :func:`decode_with` says of a ``str`` UTF-8 cannot encode.
_LONE_SURROGATE: Final = "not UTF-8 text: it holds a lone surrogate"

#: What :func:`decode_with` says of a JSON string whose bytes are not UTF-8: msgspec counts the bad byte within the
#: string that holds it, not the body, so no byte is named.
_NOT_UTF8: Final = "not UTF-8 text: a JSON string holds bytes that are not UTF-8"


def _translated[T](codec: Codec, decode: Callable[[bytes], T]) -> Callable[[bytes], T]:
    """Return *decode*, raising each of *codec*'s ``errors`` as a :class:`DecodeError` with its bounded description.

    A ``RecursionError``, which a parser such as msgspec raises for a body nested too deeply for its own stack, is a
    ``DecodeError`` too: ``nested too deeply``, which names no format, since a codec may read any. So is a
    ``UnicodeDecodeError``, which msgspec raises for a string that is not UTF-8, naming no byte. The library's
    error is not chained: it may quote the input, which a traceback or ``logger.exception`` would repeat.
    """
    errors = codec.errors

    def _decode(content: bytes) -> T:
        try:
            return decode(content)
        except errors as exc:
            raise DecodeError(shorten(codec.describe(exc))) from None
        except RecursionError:
            raise DecodeError(NESTED_TOO_DEEPLY) from None
        except UnicodeDecodeError:
            raise DecodeError(_NOT_UTF8) from None

    return _decode


def decode_with[T](codec: Codec, content: bytes | str, type_: TypeForm[T]) -> T:
    """Decode *content* into the type expression *type_* with *codec*: for content that is not a whole response.

    A ``str``, such as a server-sent event's data, is read as the bytes it is in UTF-8.

    :raises TypeError: for a *codec* that is not a :class:`Codec`, and *content* that is neither ``bytes`` nor a
        ``str``.
    :raises DecodeError: for content *codec* finds malformed or of another type, or nested too deeply to parse, and
        for a ``str`` holding a lone surrogate, which no UTF-8 holds.
    """
    check_is_codec(codec)
    check_kind("content", content, (bytes, str), "bytes or a str, such as a response's content or an event's data")
    if isinstance(content, str):
        try:
            content = content.encode()
        except UnicodeEncodeError:
            # The error quotes the text, which may carry a credential.
            raise DecodeError(_LONE_SURROGATE) from None
    return _translated(codec, codec.decoder(type_))(content)


def reader_for[T](
    codec: Codec,
    type_: TypeForm[T],
    *,
    allow_empty_body: bool = False,
    refuse: Callable[[T], RefusedError | None] | None = None,
) -> Reader[T]:
    """Build a reader that decodes the body into the type expression *type_* with *codec*.

    A body *codec* finds malformed or of another type raises :class:`~headgate.errors.DecodeError`, described by the
    codec. ``allow_empty_body`` reads an empty body as an empty array, as *codec* encodes one, for a list-shaped type
    whose provider answers "no data" with nothing; ``refuse`` runs on the decoded value, as on
    :func:`~headgate.body_reader`. The reader is named after *codec*'s class and *type_*:
    ``Reader(MsgspecCodec list[Order])``.

    :raises TypeError: for a *codec* that is not a :class:`Codec`, an ``allow_empty_body`` that is not a ``bool``, and
        a ``refuse`` that cannot be called.
    """
    check_is_codec(codec)
    check_kind("allow_empty_body", allow_empty_body, bool, "a bool")
    empty = codec.encoder(list[object])([]) if allow_empty_body else None
    decode = _translated(codec, codec.decoder(type_))
    name = f"{type(codec).__name__} {type_text(type_)}"
    return _body_reader(decode if allow_empty_body else _empty_said(decode), empty=empty, refuse=refuse, name=name)


#: What a reader that does not allow an empty body says of one, which the codec described in its parser's words.
_EMPTY_BODY: Final = "the body is empty: allow_empty_body=True reads one as an empty list"


def _empty_said[T](decode: Callable[[bytes], T]) -> Callable[[bytes], T]:
    """Return *decode*, refusing an empty (or blank) body it refuses as :data:`_EMPTY_BODY`, saying what reads one."""

    def _decode(content: bytes) -> T:
        try:
            return decode(content)
        except DecodeError:
            if content.strip():
                raise
            raise DecodeError(_EMPTY_BODY) from None

    return _decode


def writer_for[T](codec: Codec, type_: TypeForm[T]) -> Writer[T]:
    """Build a writer that encodes a value of the type expression *type_* with *codec*, sent as its ``media_type``.

    :raises TypeError: for a *codec* that is not a :class:`Codec`.
    """
    check_is_codec(codec)
    encode = codec.encoder(type_)
    media_type = codec.media_type

    def _write(value: T, /) -> EncodedBody:
        return EncodedBody(encode(value), media_type)

    return _write


def drop_none(obj: object) -> object:
    """Return *obj* with every ``None``-valued key removed, walking nested dicts, lists and tuples.

    Every other value survives, so ``False``, ``0`` and ``""`` stay (unlike msgspec's ``omit_defaults``; like
    pydantic's ``exclude_none``). Insertion order is preserved. Tuples are walked because ``msgspec.to_builtins`` keeps
    a ``tuple`` field as a tuple, and the structs inside one have ``None`` fields like any other.
    """
    if isinstance(obj, dict):
        return {k: drop_none(v) for k, v in obj.items() if v is not None}
    if isinstance(obj, list):
        return [drop_none(v) for v in obj]
    if isinstance(obj, tuple):
        return tuple(drop_none(v) for v in obj)
    return obj
