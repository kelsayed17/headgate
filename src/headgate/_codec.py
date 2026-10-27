# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""Codecs: what headgate needs of a serialization library, and the readers and writers built on one.

The core imports no serialization library. A :class:`Codec` is the one interface between them: how the library reads
bytes into a type expression and writes a value back, the media type of its bytes, which of its exceptions mean a body
of the wrong shape, and how to describe one without repeating the input. :func:`decoder_for` and :func:`encoder_for`
build a reader and a writer on any codec, and :func:`decode_with` decodes bytes that are not a whole response.

``headgate.msgspec`` and ``headgate.pydantic`` are two codecs. :func:`headgate.testing.check_codec` holds any codec to
the promises theirs keep: strict types, unknown fields ignored, malformed bytes refused, and messages that never quote
the input.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from headgate._failures import shorten
from headgate._request_body import EncodedBody
from headgate._responses import reader_over
from headgate.errors import DecodeError

if TYPE_CHECKING:
    from collections.abc import Callable

    from typing_extensions import TypeForm

    from headgate._request_body import Writer
    from headgate._responses import Reader, RefusedError

__all__ = ["Codec", "decode_with", "decoder_for", "drop_none", "encoder_for"]


class Codec(Protocol):
    """A serialization library, as headgate uses one.

    * ``media_type``: what its bytes are (``"application/json"``, ``"application/msgpack"``): the ``Content-Type`` a
      writer sends.
    * ``errors``: the exceptions it raises for bytes that are malformed or do not match the type. A reader turns them
      into :class:`~headgate.errors.DecodeError`, which a call raises as ``ResponseParseError``, so a library's own
      validation error never escapes the ``ClientError`` tree.
    * ``decoder(type_)`` and ``encoder(type_)``: a function reading bytes into the type expression, and one writing a
      value of it back. Each is built once per type, so a library that compiles a type (a msgspec decoder, a pydantic
      adapter) does so once.
    * ``describe(error)``: a message for one of ``errors`` that never quotes the input, since the body may carry a
      credential. The reader bounds its length.
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


def _translated[T](codec: Codec, decode: Callable[[bytes], T]) -> Callable[[bytes], T]:
    """Return *decode*, raising each of *codec*'s ``errors`` as a :class:`DecodeError` with its bounded description."""
    errors = codec.errors

    def _decode(content: bytes) -> T:
        try:
            return decode(content)
        except errors as exc:
            raise DecodeError(shorten(codec.describe(exc))) from exc

    return _decode


def decode_with[T](codec: Codec, content: bytes, type_: TypeForm[T]) -> T:
    """Decode *content* into the type expression *type_* with *codec*: for bytes that are not a whole response.

    :raises DecodeError: for bytes *codec* finds malformed or of another type.
    """
    return _translated(codec, codec.decoder(type_))(content)


def decoder_for[T](
    codec: Codec,
    type_: TypeForm[T],
    *,
    allow_empty_body: bool = False,
    refusal_check: Callable[[T], RefusedError | None] | None = None,
) -> Reader[T]:
    """Build a reader that decodes the body into the type expression *type_* with *codec*.

    A body *codec* finds malformed or of another type raises :class:`~headgate.errors.DecodeError`, described by the
    codec. ``allow_empty_body`` reads an empty body as an empty array, as *codec* encodes one, for a list-shaped type
    whose provider answers "no data" with nothing; ``refusal_check`` runs on the decoded value, as on
    :func:`~headgate.json_reader`.
    """
    empty = codec.encoder(list[object])([]) if allow_empty_body else None
    return reader_over(_translated(codec, codec.decoder(type_)), empty=empty, refusal_check=refusal_check)


def encoder_for[T](codec: Codec, type_: TypeForm[T]) -> Writer[T]:
    """Build a writer that encodes a value of the type expression *type_* with *codec*, sent as its ``media_type``."""
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
