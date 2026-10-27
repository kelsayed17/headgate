# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""The msgspec codec: strict JSON decoding into ``msgspec.Struct`` types, and request bodies written back.

:data:`CODEC` is msgspec as a :class:`~headgate.Codec`. A client usually reaches it through the shortcuts here
(``from headgate.msgspec import decoder, encoder``): :func:`decode` is the bytes-level primitive, :func:`decoder`
builds a response reader on it and :func:`encoder` its mirror, a request-body writer; :func:`to_builtins` prepares
builtins for ``json=`` or ``data=``. Every decode failure is the shared :class:`~headgate.errors.DecodeError`.

A ``DecodeError``'s message is msgspec's own diagnosis (the expected type and the path), bounded, with the one input
value msgspec quotes (an unknown enum value) left out.

Drift detection is strict **typing**, not unknown-field rejection. ``strict=True`` catches a field that changes type
on the wire, which silently corrupts values. ``forbid_unknown_fields`` would catch a field appearing, which providers
do routinely and without notice: rejecting one turns a harmless upstream addition into a failure on every call, while
ignoring it costs nothing, since a field nobody models is a field nobody reads. A struct can still opt in with
``forbid_unknown_fields=True`` where noticing a new field is worth that risk.

``type_`` parameters are annotated ``TypeForm[T]`` (PEP 747), so a union or a generic alias such as ``list[A] | A``
types the result as exactly that expression.

Requires the ``msgspec`` extra: ``pip install headgate[msgspec]``.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, ClassVar, Final, overload

try:
    import msgspec
except ImportError as exc:
    msg = "headgate.msgspec needs msgspec: pip install headgate[msgspec]"
    raise ImportError(msg) from exc

from headgate._codec import decode_with, decoder_for, encoder_for
from headgate._codec import drop_none as _drop_none

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from typing_extensions import TypeForm

    from headgate._request_body import Writer
    from headgate._responses import Reader, RefusedError

__all__ = ["CODEC", "MsgspecCodec", "decode", "decoder", "encoder", "to_builtins"]

#: One encoder serves every value: msgspec encodes by the value's own type, so it holds no per-type state.
_JSON_ENCODER: Final = msgspec.json.Encoder()

#: The one msgspec diagnosis that quotes the input: ``Invalid enum value '<the value>'``, whatever its length, before
#: the `` - at `$…` `` path when there is one.
_ENUM_VALUE = re.compile(r"(Invalid enum value) .*?(?= - at `\$|$)", re.DOTALL)


class MsgspecCodec:
    """JSON through msgspec, as a :class:`~headgate.Codec`.

    *strict* (the default) rejects msgspec's built-in coercions, so a JSON string where an ``int`` is declared is wire
    drift; ``strict=False`` is the deliberate opt-out for a wire that needs lax coercion. *drop_none* leaves
    ``None``-valued fields out of what it encodes, recursively, as :func:`to_builtins` does.
    """

    __slots__ = ("_drop_none", "_strict")

    media_type: ClassVar[str] = "application/json"
    #: ``msgspec.ValidationError`` subclasses ``msgspec.DecodeError``, so the base covers malformed and mistyped bytes.
    errors: ClassVar[tuple[type[Exception], ...]] = (msgspec.DecodeError,)

    def __init__(self, *, strict: bool = True, drop_none: bool = False) -> None:
        """Decode strictly unless *strict* is false; encode without ``None`` fields when *drop_none* is true."""
        self._strict = strict
        self._drop_none = drop_none

    def decoder[T](self, type_: TypeForm[T], /) -> Callable[[bytes], T]:
        """Return a ``msgspec.json.Decoder`` for *type_*'s decode, built once.

        msgspec's stubs type a non-``type[T]`` argument as ``Any``; the annotation records that the decoder decodes into
        exactly the type expression it was built from.
        """
        json_decoder: msgspec.json.Decoder[T] = msgspec.json.Decoder(type_, strict=self._strict)
        return json_decoder.decode

    def encoder[T](self, type_: TypeForm[T], /) -> Callable[[T], bytes]:
        """Return the function that encodes a value of *type_*: msgspec encodes by the value's own type."""
        del type_  # the expression only types the writer
        drop_none = self._drop_none

        def encode(value: T) -> bytes:
            return _JSON_ENCODER.encode(_drop_none(msgspec.to_builtins(value)) if drop_none else value)

        return encode

    def describe(self, error: Exception, /) -> str:
        """Return msgspec's diagnosis with any quoted input value dropped."""
        return _ENUM_VALUE.sub(r"\1", str(error))


#: msgspec, decoding strictly: the codec every shortcut here uses unless told otherwise.
CODEC: Final = MsgspecCodec()

_LAX: Final = MsgspecCodec(strict=False)
_DROPPING_NONE: Final = MsgspecCodec(drop_none=True)


def decode[T](content: bytes, type_: TypeForm[T], *, strict: bool = True) -> T:
    """Decode *content* as strict JSON into the type expression *type_*.

    The primitive every JSON reader shares, and the one to call for bytes that are not a whole response: a document
    nested inside another, or a fixture read from disk. ``strict=True`` rejects msgspec's built-in coercions, so a JSON
    string where an ``int`` is declared is wire drift.

    :raises DecodeError: if *content* is not JSON or does not match *type_*.
    """
    return decode_with(CODEC if strict else _LAX, content, type_)


def decoder[T](
    type_: TypeForm[T],
    *,
    strict: bool = True,
    allow_empty_body: bool = False,
    refusal_check: Callable[[T], RefusedError | None] | None = None,
) -> Reader[T]:
    """Build a reader that decodes the body as strict JSON into the type expression *type_*.

    *type_* carries the whole wire shape, so one function serves every JSON body: an enveloped object
    (``decoder(MyResponse)``), a bare array (``decoder(list[MyRow])``), a bare map (``decoder(dict[str, MyRow])``), or
    a union where a provider's body is genuinely one shape or the other (``decoder(list[A] | A)``).

    msgspec decodes the expression atomically, so one malformed element fails the whole body with
    :class:`~headgate.errors.DecodeError`. ``strict=False`` is the deliberate opt-out for a wire that needs lax
    coercion. ``allow_empty_body`` and ``refusal_check`` are documented on :func:`~headgate.decoder_for`.
    """
    codec = CODEC if strict else _LAX
    return decoder_for(codec, type_, allow_empty_body=allow_empty_body, refusal_check=refusal_check)


def encoder[T](type_: TypeForm[T], *, drop_none: bool = False) -> Writer[T]:
    """Build a writer that encodes a value of the type expression *type_* as a JSON request body.

    The mirror of :func:`decoder`: build it once, at import (``NEW_ORDER = encoder(NewOrder)``), and send with
    ``body=NEW_ORDER(order)``. msgspec encodes the value straight to bytes, with the same renames, field order and
    ``omit_defaults`` as :func:`to_builtins`; *type_* types the writer, so it takes only that type. ``drop_none=True``
    removes ``None``-valued fields recursively, as :func:`to_builtins` does.
    """
    return encoder_for(_DROPPING_NONE if drop_none else CODEC, type_)


@overload
def to_builtins(value: msgspec.Struct | Mapping[str, object], *, drop_none: bool = ...) -> dict[str, object]: ...
@overload
def to_builtins[T](value: list[T], *, drop_none: bool = ...) -> list[object]: ...
@overload
def to_builtins(value: object, *, drop_none: bool = ...) -> object: ...
def to_builtins(value: object, *, drop_none: bool = False) -> object:
    """Convert a struct, or a list or mapping of them, to builtins ready for httpx2's ``json=`` or ``data=``.

    A delegate of ``msgspec.to_builtins``: it applies each struct's ``field(name=...)`` renames, keeps field
    declaration order (the wire key order once httpx2 serializes), and honors ``omit_defaults``. An ``array_like``
    struct converts to a list, not the ``dict`` its overload names.

    ``drop_none=True`` also removes ``None``-valued fields recursively, for a body whose unset optionals must be
    omitted rather than sent as ``null``. It keeps ``False`` and ``0``, unlike ``omit_defaults``.
    """
    out = msgspec.to_builtins(value)
    return _drop_none(out) if drop_none else out
