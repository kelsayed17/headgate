# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""The implementation of :mod:`headgate.msgspec`, which exports it and documents it."""

import re
from typing import ClassVar, Final, Literal, cast, overload

try:
    import msgspec
    import msgspec.inspect
except ImportError as exc:
    msg = "headgate.msgspec needs msgspec: pip install headgate[msgspec]"
    raise ImportError(msg) from exc

from collections.abc import Callable, Mapping

from typing_extensions import TypeForm

from headgate._codec import decode_with, reader_for, writer_for
from headgate._codec import drop_none as _drop_none
from headgate._core import check_kind
from headgate._request_body import Writer
from headgate._responses import Reader
from headgate.errors import RefusedError

__all__ = ["CODEC", "MsgspecCodec", "decode", "reader", "to_builtins", "writer"]

#: One encoder serves every value: msgspec encodes by the value's own type, so it holds no per-type state.
_JSON_ENCODER: Final = msgspec.json.Encoder()

#: The msgspec diagnoses that quote the input: an unknown enum value or tag (``Invalid enum value 'x'``, ``Invalid value
#: 'x'``) and an unknown field's name (``Object contains unknown field `x```). What they quote runs, whatever it holds,
#: up to the path msgspec ends the message with when there is one (`` - at `$.a` ``, `` - at `key` in `$` ``).
_QUOTED_INPUT = re.compile(
    r"(Invalid (?:enum )?value|Object contains unknown field) .*?(?=(?: - at `[^`]*`(?: in `[^`]*`)?)?\Z)", re.DOTALL
)


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
        """Decode strictly unless *strict* is false; encode without ``None`` fields when *drop_none* is true.

        :raises TypeError: for a *strict* or *drop_none* that is not a ``bool``.
        """
        check_kind("strict", strict, bool, "a bool")
        check_kind("drop_none", drop_none, bool, "a bool")
        self._strict = strict
        self._drop_none = drop_none

    def decoder[T](self, type_: TypeForm[T], /) -> Callable[[bytes], T]:
        """Return a ``msgspec.json.Decoder`` for *type_*'s decode, built once.

        msgspec's stubs type a non-``type[T]`` argument as ``Any``; the cast records that the decoder decodes into
        exactly the type expression it was built from.

        :raises TypeError: for a type that holds, anywhere, a class msgspec does not know (a pydantic model, a plain
            class), which msgspec would build a decoder for and then refuse every body with: here, naming the class.
        """
        _refuse_unknown_classes(type_, "decode")
        json_decoder = cast("msgspec.json.Decoder[T]", msgspec.json.Decoder(type_, strict=self._strict))
        return json_decoder.decode

    def encoder[T](self, type_: TypeForm[T], /) -> Callable[[T], bytes]:
        """Return the function that encodes a value of *type_*: msgspec encodes by the value's own type.

        :raises TypeError: for a type that holds, anywhere, a class msgspec does not know, which no value of could be
            encoded: here, naming the class, as :meth:`decoder` refuses it.
        """
        _refuse_unknown_classes(type_, "encode")
        drop_none = self._drop_none

        def encode(value: T) -> bytes:
            return _JSON_ENCODER.encode(_drop_none(msgspec.to_builtins(value)) if drop_none else value)

        return encode

    def describe(self, error: Exception, /) -> str:
        """Return msgspec's diagnosis with any input it quotes dropped."""
        return _QUOTED_INPUT.sub(r"\1", str(error))


def _refuse_unknown_classes(type_: object, verb: Literal["decode", "encode"]) -> None:
    """Refuse *type_* when it holds a class msgspec reports as a custom type, which it cannot *verb* without a hook.

    ``object`` is a custom type to :func:`msgspec.inspect.type_info` too, but msgspec decodes it as any value. The
    walk keeps the nodes it has seen, so a recursive struct ends.

    :raises TypeError: naming the class: a pydantic model is pointed to the pydantic flavor.
    """
    pending: list[object] = [msgspec.inspect.type_info(type_)]
    seen: set[int] = set()
    while pending:
        node = pending.pop()
        if id(node) in seen:
            continue
        seen.add(id(node))
        if isinstance(node, msgspec.inspect.CustomType):
            if node.cls is not object:
                raise TypeError(_unknown_class(node.cls, verb))
            continue
        for field in msgspec.structs.fields(cast("msgspec.Struct", node)):
            value = getattr(node, field.name)
            items = value if isinstance(value, tuple) else (value,)
            pending.extend(item for item in items if isinstance(item, msgspec.inspect.Type | msgspec.inspect.Field))


def _unknown_class(cls: type, verb: Literal["decode", "encode"]) -> str:
    """Describe *cls*, a class headgate.msgspec cannot *verb*, and what to write instead."""
    name = cls.__name__
    builder = "reader" if verb == "decode" else "writer"
    if any(base.__module__.partition(".")[0] == "pydantic" for base in cls.__mro__):
        return f"headgate.msgspec cannot {verb} {name}, a pydantic model: use headgate.pydantic.{builder}({name})"
    by_hand = (
        f"read it with Reader(lambda r: {name}(...))" if verb == "decode" else "write it with a Writer of your own"
    )
    return (
        f"headgate.msgspec cannot {verb} {name}, a class msgspec does not know: declare it as a msgspec.Struct, a "
        f"dataclass or a TypedDict, or {by_hand}"
    )


#: msgspec, decoding strictly: the codec every shortcut here uses unless told otherwise.
CODEC: Final = MsgspecCodec()

_LAX: Final = MsgspecCodec(strict=False)
_DROPPING_NONE: Final = MsgspecCodec(drop_none=True)


def decode[T](content: bytes | str, type_: TypeForm[T], *, strict: bool = True) -> T:
    """Decode *content* as strict JSON into the type expression *type_*.

    The primitive every JSON reader shares, and the one to call for content that is not a whole response: a document
    nested inside another, a fixture read from disk, or a server-sent event's data, a ``str`` read as its UTF-8 bytes.
    ``strict=True`` rejects msgspec's built-in coercions, so a JSON string where an ``int`` is declared is wire drift.

    :raises TypeError: for *content* that is neither ``bytes`` nor a ``str``, and a *strict* that is not a ``bool``.
    :raises DecodeError: if *content* is not JSON or does not match *type_*.
    """
    check_kind("strict", strict, bool, "a bool")
    return decode_with(CODEC if strict else _LAX, content, type_)


def reader[T](
    type_: TypeForm[T],
    *,
    strict: bool = True,
    allow_empty_body: bool = False,
    refuse: Callable[[T], RefusedError | None] | None = None,
) -> Reader[T]:
    """Build a reader that decodes the body as strict JSON into the type expression *type_*.

    *type_* carries the whole wire shape, so one function serves every JSON body: an enveloped object
    (``reader(MyResponse)``), a bare array (``reader(list[MyRow])``), a bare map (``reader(dict[str, MyRow])``), or a
    union where a provider's body is genuinely one shape or the other (``reader(list[A] | A)``).

    msgspec decodes the expression atomically, so one malformed element fails the whole body with
    :class:`~headgate.errors.DecodeError`. ``strict=False`` is the deliberate opt-out for a wire that needs lax
    coercion. ``allow_empty_body`` and ``refuse`` are documented on :func:`~headgate.reader_for`.
    """
    check_kind("strict", strict, bool, "a bool")
    codec = CODEC if strict else _LAX
    return reader_for(codec, type_, allow_empty_body=allow_empty_body, refuse=refuse)


def writer[T](type_: TypeForm[T], *, drop_none: bool = False) -> Writer[T]:
    """Build a writer that encodes a value of the type expression *type_* as a JSON request body.

    The mirror of :func:`reader`: build it once, at import (``NEW_ORDER = writer(NewOrder)``), and send with
    ``body=NEW_ORDER(order)``. msgspec encodes the value straight to bytes, with the same renames, field order and
    ``omit_defaults`` as :func:`to_builtins`; *type_* types the writer, so it takes only that type. ``drop_none=True``
    removes ``None``-valued fields recursively, as :func:`to_builtins` does.
    """
    check_kind("drop_none", drop_none, bool, "a bool")
    return writer_for(_DROPPING_NONE if drop_none else CODEC, type_)


@overload
def to_builtins(value: msgspec.Struct | Mapping[str, object], *, drop_none: bool = ...) -> dict[str, object]: ...
@overload
def to_builtins[T](value: list[T], *, drop_none: bool = ...) -> list[object]: ...
@overload
def to_builtins(value: object, *, drop_none: bool = ...) -> object: ...
def to_builtins(value: object, *, drop_none: bool = False) -> object:
    """Convert a struct, or a list or mapping of them, to builtins ready for ``body=Json(...)`` or ``Form(...)``.

    A delegate of ``msgspec.to_builtins``: it applies each struct's ``field(name=...)`` renames, keeps field
    declaration order (the wire key order once httpx2 serializes), and honors ``omit_defaults``. An ``array_like``
    struct converts to a list, not the ``dict`` its overload names.

    ``drop_none=True`` also removes ``None``-valued fields recursively, for a body whose unset optionals must be
    omitted rather than sent as ``null``. It keeps ``False`` and ``0``, unlike ``omit_defaults``.
    """
    check_kind("drop_none", drop_none, bool, "a bool")
    out = msgspec.to_builtins(value)
    return _drop_none(out) if drop_none else out
