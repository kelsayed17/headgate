# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""The pydantic codec flavor: strict JSON validation into pydantic models, and request-body preparation.

The same surface as :mod:`headgate.msgspec`, so switching flavor is changing one import
(``from headgate.pydantic import decoder, to_builtins``). Wire models are frozen, strict ``BaseModel`` classes::

    class Row(BaseModel):
        model_config = ConfigDict(strict=True, extra="ignore", frozen=True)

Strictness parity with the msgspec flavor is held by tests: a JSON string where an ``int`` is declared fails, an
unknown field is ignored, and every failure is the shared :class:`~headgate.errors.DecodeError`.

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
from headgate._responses import json_reader
from headgate.errors import DecodeError

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from typing_extensions import TypeForm

    from headgate._responses import Reader, RefusedError

__all__ = ["decode", "decoder", "to_builtins"]


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
        raise DecodeError(str(exc)) from exc


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
    check: Callable[[T], RefusedError | None] | None = None,
) -> Reader[T]:
    """Build a reader that validates the body as strict JSON against the type expression *type_*.

    Accepts the same shapes as :func:`headgate.msgspec.decoder` (an enveloped model, ``list[Row]``,
    ``dict[str, Row]``, a union of shapes). One malformed element fails the whole body. ``allow_empty_body`` and
    ``check`` are documented on :func:`~headgate._responses.json_reader`.
    """
    decode_body = functools.partial(_validate_with, _adapter(type_), strict=strict)
    return json_reader(decode_body, allow_empty_body=allow_empty_body, check=check)


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
