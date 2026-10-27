# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""The pydantic codec flavor: strict JSON validation into pydantic models, and request-body preparation.

The same surface as :mod:`headgate.msgspec`, so switching flavor is changing one import
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
from typing import TYPE_CHECKING, Any, cast, overload

try:
    from pydantic import BaseModel, RootModel, TypeAdapter, ValidationError
    from pydantic_core import to_jsonable_python
except ImportError as exc:
    msg = "headgate.pydantic needs pydantic: pip install headgate[pydantic]"
    raise ImportError(msg) from exc

from headgate._codec import drop_none as _drop_none
from headgate._failures import shorten
from headgate._request_body import json_writer
from headgate._responses import json_reader
from headgate.errors import DecodeError

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from typing_extensions import TypeForm

    from headgate._request_body import Writer
    from headgate._responses import Reader, RefusedError

__all__ = ["decode", "decoder", "encoder", "to_builtins"]

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


def _validate_with[T](adapter: TypeAdapter[T], content: bytes, *, strict: bool) -> T:
    """Validate *content* as JSON with *adapter*, raising :class:`DecodeError` for malformed or mistyped JSON."""
    try:
        return adapter.validate_json(content, strict=strict)
    except ValidationError as exc:
        raise DecodeError(_describe(exc)) from exc


def _describe(exc: ValidationError) -> str:
    """Summarize *exc* without any input value: its first :data:`_ERRORS_SHOWN` failures, and the count, bounded."""
    errors = exc.errors(include_input=False, include_url=False, include_context=False)[:_ERRORS_SHOWN]
    shown = "; ".join(f"{'.'.join(map(str, error['loc'])) or '<body>'}: {error['msg']}" for error in errors)
    count = exc.error_count()
    more = f" (+{count - len(errors)} more)" if count > len(errors) else ""
    noun = "error" if count == 1 else "errors"
    return shorten(f"{count} validation {noun} for {exc.title}: {shown}{more}")


def decode[T](content: bytes, type_: TypeForm[T], *, strict: bool = True) -> T:
    """Validate *content* as strict JSON against the type expression *type_*.

    ``strict=True`` rejects pydantic's lax coercions, so a JSON string where an ``int`` is declared is wire drift.
    Invalid JSON and a type mismatch raise the same error.

    :raises DecodeError: if *content* is not JSON or does not match *type_*.
    """
    return _validate_with(_adapter(type_), content, strict=strict)


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
    ``refusal_check`` are documented on :func:`~headgate._responses.json_reader`.
    """
    decode_body = functools.partial(_validate_with, _adapter(type_), strict=strict)
    return json_reader(decode_body, allow_empty_body=allow_empty_body, refusal_check=refusal_check)


def encoder[T](type_: TypeForm[T], *, drop_none: bool = False) -> Writer[T]:
    """Build a writer that encodes a value of the type expression *type_* as a JSON request body.

    The mirror of :func:`decoder`: build it once, at import (``NEW_ORDER = encoder(NewOrder)``), and send with
    ``body=NEW_ORDER(order)``. The value is serialized by *type_*'s ``TypeAdapter``, with field aliases applied, as
    :func:`to_builtins` applies them. ``drop_none=True`` leaves out ``None``-valued fields.
    """
    adapter = _adapter(type_)

    def encode(value: T) -> bytes:
        return adapter.dump_json(value, by_alias=True, exclude_none=drop_none)

    return json_writer(encode)


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
