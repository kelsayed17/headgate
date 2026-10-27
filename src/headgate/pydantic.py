# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""The pydantic codec: strict JSON validation into pydantic models, and request bodies written back.

:data:`CODEC` is pydantic as a :class:`~headgate.Codec`, with the same shortcuts as :mod:`headgate.msgspec`, so
switching codec is changing one import
(``from headgate.pydantic import decoder, encoder, to_builtins``). Wire models are frozen, strict ``BaseModel``
classes::

    class Row(BaseModel):
        model_config = ConfigDict(strict=True, extra="ignore", frozen=True)

Strictness parity with the msgspec flavor is held by tests: a JSON string where an ``int`` is declared fails, an
unknown field is ignored, and every failure is the shared :class:`~headgate.errors.DecodeError`.

A ``DecodeError``'s message names the first few failures by location and reason, and how many there were. It never
repeats an input value: ``str(ValidationError)`` lists every failure with the value that failed, so a body of 5,000 bad
rows makes a message of most of a megabyte, and a token field that failed validation is quoted back in full.

Requires the ``pydantic`` extra: ``pip install headgate[pydantic]``.
"""

from __future__ import annotations

import functools
from typing import TYPE_CHECKING, Any, ClassVar, Final, cast, overload

try:
    from pydantic import BaseModel, RootModel, TypeAdapter, ValidationError
    from pydantic_core import to_jsonable_python
except ImportError as exc:
    msg = "headgate.pydantic needs pydantic: pip install headgate[pydantic]"
    raise ImportError(msg) from exc

from headgate._codec import decode_with, decoder_for, encoder_for
from headgate._codec import drop_none as _drop_none

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from typing_extensions import TypeForm

    from headgate._request_body import Writer
    from headgate._responses import Reader, RefusedError

__all__ = ["CODEC", "PydanticCodec", "decode", "decoder", "encoder", "to_builtins"]

#: How many of a validation's failures a ``DecodeError`` message names; the count of the rest follows them.
_ERRORS_SHOWN = 5


@functools.cache
def _cached_adapter(type_: TypeForm[object]) -> TypeAdapter[Any]:
    """Return the one ``TypeAdapter`` for *type_*, built on first use.

    ``decoder`` typically runs on every request, and :func:`decode` may run once per element of a larger document, so
    each type expression is compiled once. A cache shared by every type expression cannot carry each entry's type
    parameter, which is why it returns ``TypeAdapter[Any]``; :func:`_adapter` restores it.
    """
    return TypeAdapter(type_)


def _adapter[T](type_: TypeForm[T]) -> TypeAdapter[T]:
    """Return the cached ``TypeAdapter`` for the type expression *type_*, typed by it."""
    # The cache key is the type expression itself, so the entry it returns was built from exactly ``TypeForm[T]``.
    return cast("TypeAdapter[T]", _cached_adapter(type_))


class PydanticCodec:
    """JSON through pydantic, as a :class:`~headgate.Codec`: each type expression's cached ``TypeAdapter``.

    *strict* (the default) rejects pydantic's lax coercions, so a JSON string where an ``int`` is declared is wire
    drift. Field aliases are the wire's names both ways. *drop_none* leaves ``None``-valued fields out of what it
    encodes.
    """

    __slots__ = ("_drop_none", "_strict")

    media_type: ClassVar[str] = "application/json"
    #: Invalid JSON and a type mismatch both raise it.
    errors: ClassVar[tuple[type[Exception], ...]] = (ValidationError,)

    def __init__(self, *, strict: bool = True, drop_none: bool = False) -> None:
        """Validate strictly unless *strict* is false; encode without ``None`` fields when *drop_none* is true."""
        self._strict = strict
        self._drop_none = drop_none

    def decoder[T](self, type_: TypeForm[T], /) -> Callable[[bytes], T]:
        """Return a function validating JSON against *type_*'s cached adapter."""
        adapter = _adapter(type_)
        strict = self._strict

        def decode(content: bytes) -> T:
            return adapter.validate_json(content, strict=strict)

        return decode

    def encoder[T](self, type_: TypeForm[T], /) -> Callable[[T], bytes]:
        """Return a function serializing a value by *type_*'s cached adapter, by alias."""
        adapter = _adapter(type_)
        drop_none = self._drop_none

        def encode(value: T) -> bytes:
            return adapter.dump_json(value, by_alias=True, exclude_none=drop_none)

        return encode

    def describe(self, error: Exception, /) -> str:
        """Summarize a ``ValidationError`` without any input value: its first failures, and how many there were."""
        exc = cast("ValidationError", error)  # the only one of :attr:`errors`
        errors = exc.errors(include_input=False, include_url=False, include_context=False)[:_ERRORS_SHOWN]
        shown = "; ".join(f"{'.'.join(map(str, error['loc'])) or '<body>'}: {error['msg']}" for error in errors)
        count = exc.error_count()
        more = f" (+{count - len(errors)} more)" if count > len(errors) else ""
        noun = "error" if count == 1 else "errors"
        return f"{count} validation {noun} for {exc.title}: {shown}{more}"


#: pydantic, validating strictly: the codec every shortcut here uses unless told otherwise.
CODEC: Final = PydanticCodec()

_LAX: Final = PydanticCodec(strict=False)
_DROPPING_NONE: Final = PydanticCodec(drop_none=True)


def decode[T](content: bytes, type_: TypeForm[T], *, strict: bool = True) -> T:
    """Validate *content* as strict JSON against the type expression *type_*.

    ``strict=True`` rejects pydantic's lax coercions, so a JSON string where an ``int`` is declared is wire drift.
    Invalid JSON and a type mismatch raise the same error.

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
    """Build a reader that validates the body as strict JSON against the type expression *type_*.

    Accepts the same shapes as :func:`headgate.msgspec.decoder` (an enveloped model, ``list[Row]``,
    ``dict[str, Row]``, a union of shapes). One malformed element fails the whole body. ``allow_empty_body`` and
    ``refusal_check`` are documented on :func:`~headgate.decoder_for`.
    """
    codec = CODEC if strict else _LAX
    return decoder_for(codec, type_, allow_empty_body=allow_empty_body, refusal_check=refusal_check)


def encoder[T](type_: TypeForm[T], *, drop_none: bool = False) -> Writer[T]:
    """Build a writer that encodes a value of the type expression *type_* as a JSON request body.

    The mirror of :func:`decoder`: build it once, at import (``NEW_ORDER = encoder(NewOrder)``), and send with
    ``body=NEW_ORDER(order)``. The value is serialized by *type_*'s ``TypeAdapter``, with field aliases applied, as
    :func:`to_builtins` applies them. ``drop_none=True`` leaves out ``None``-valued fields.
    """
    return encoder_for(_DROPPING_NONE if drop_none else CODEC, type_)


@overload
def to_builtins(value: RootModel[Any], *, drop_none: bool = ...) -> object: ...
@overload
def to_builtins(value: BaseModel | Mapping[str, object], *, drop_none: bool = ...) -> dict[str, object]: ...
@overload
def to_builtins[T](value: list[T], *, drop_none: bool = ...) -> list[object]: ...
@overload
def to_builtins(value: object, *, drop_none: bool = ...) -> object: ...
def to_builtins(value: object, *, drop_none: bool = False) -> object:
    """Convert a model, or a list or mapping of them, to builtins ready for httpx2's ``json=`` or ``data=``.

    Field aliases are applied (``by_alias=True``), so a model declares the wire's names once. A ``RootModel``
    converts to whatever its root converts to. ``drop_none=True`` also removes ``None``-valued fields recursively,
    keeping ``False`` and ``0``.
    """
    out = to_jsonable_python(value, by_alias=True)
    return _drop_none(out) if drop_none else out
