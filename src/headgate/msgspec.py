# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""The msgspec codec flavor: strict JSON decode into ``msgspec.Struct``s, and request-body preparation.

A client picks its flavor by import, ``from headgate.msgspec import decoder, to_builtins``, and the base client stays a
pure HTTP-policy layer with no serialization knowledge of its own.

:func:`decode` is the strict bytes-level primitive, :func:`decoder` builds a response reader on it, and
:func:`to_builtins` prepares a request body. Every failure to decode is the shared
:class:`~headgate.errors.DecodeError`.

**Drift detection is strict TYPING, not unknown-field rejection**, and that asymmetry is a decision rather than an
oversight. ``strict=True`` catches a field that CHANGES type on the wire, which silently corrupts values;
``forbid_unknown_fields`` catches a field APPEARING, which providers do routinely and without notice. Rejecting one
turns a harmless upstream addition into a failure on every call to that endpoint, while ignoring it costs nothing, a
field we do not model being a field we do not read. A struct can opt in with ``forbid_unknown_fields=True`` where
noticing a new field is worth the outage risk.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, overload

import msgspec

from headgate._codec import drop_none as _drop_none
from headgate._responses import json_reader
from headgate.errors import DecodeError

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import UnionType

    from headgate._responses import Reader, Refused

__all__ = ["decode", "decoder", "to_builtins"]


@overload
def decode[T](content: bytes, type_: type[T], *, strict: bool = ...) -> T: ...
@overload
def decode(content: bytes, type_: UnionType, *, strict: bool = ...) -> Any: ...  # a union pins no type
@overload
def decode(content: bytes, type_: type[Any] | UnionType, *, strict: bool = ...) -> Any: ...  # as above
def decode[T](content: bytes, type_: type[T] | UnionType, *, strict: bool = True) -> T | Any:
    """Strict msgspec JSON decode of *content* into *type_*, raising :class:`DecodeError` on failure.

    The bytes-level primitive every JSON reader shares, and the one to call for bytes that are not a whole response: a
    document nested inside another, or a fixture read from disk. ``strict=True`` rejects msgspec's built-in coercions,
    so a JSON string where an ``int`` is declared is wire drift and fails loud.

    The third overload serves a caller holding a ``type[T] | UnionType`` (``decoder`` itself), which a type checker
    cannot narrow across the first two.
    """
    try:
        return msgspec.json.decode(content, type=type_, strict=strict)
    # msgspec.ValidationError subclasses msgspec.DecodeError, so catching the base alone covers both. Do NOT
    # "simplify" this to imply only one is raised.
    except msgspec.DecodeError as exc:
        raise DecodeError(str(exc)) from exc


@overload
def decoder[T](
    type_: type[T],
    *,
    strict: bool = ...,
    allow_empty_body: bool = ...,
    check: Callable[[T], Refused | None] | None = ...,
) -> Reader[T]: ...
@overload
def decoder(type_: UnionType, *, strict: bool = ..., allow_empty_body: bool = ...) -> Reader[Any]: ...
def decoder[T](
    type_: type[T] | UnionType,
    *,
    strict: bool = True,
    allow_empty_body: bool = False,
    check: Callable[[T], Refused | None] | None = None,
) -> Reader[T] | Reader[Any]:
    """Build a reader that decodes the body as strict msgspec JSON into the type expression ``type_``.

    ``type_`` carries the whole wire shape, so one function serves every JSON body:

    * an enveloped object: ``decoder(MyResponse)``;
    * a bare top-level array of objects with no envelope Struct: ``decoder(list[MyRow])``;
    * a bare map: ``decoder(dict[str, MySnapshot])``;
    * an array of string arrays: ``decoder(list[list[str]])``;
    * a UNION of shapes, where a provider's body is genuinely one OR the other: ``decoder(list[A] | A)``. That arm
      returns ``Reader[Any]``, since a union is not a ``type[T]``, and the caller's declared return annotation pins the
      shape.

    msgspec decodes the expression atomically, so one malformed element fails the whole decode with
    :class:`~headgate.errors.DecodeError`. ``strict=True`` (the default) makes a coerced token wire drift;
    ``strict=False`` is the deliberate opt-out for a wire that needs lax coercion. ``allow_empty_body`` and ``check``
    are documented on :func:`~headgate._responses.json_reader`.
    """
    return json_reader(
        lambda content: decode(content, type_, strict=strict), allow_empty_body=allow_empty_body, check=check
    )


def to_builtins(value: object, *, drop_none: bool = False) -> Any:  # JSON builtins for httpx2 json=/data=
    """Convert a msgspec struct (or sequence of structs) to ``json=``-ready builtins.

    A thin ``msgspec.to_builtins`` delegate that applies the struct's ``field(name=…)`` renames, preserves
    field-declaration order (dict order is wire key order once httpx2 serializes), and honors ``omit_defaults``.

    ``drop_none=True`` additionally removes ``None``-valued fields recursively, for outbound bodies whose unset
    optionals must be OMITTED rather than serialized as ``null``. The drop keeps ``False`` (unlike ``omit_defaults``).

    Serves BOTH body wire formats: JSON bodies pass the builtins to httpx2's ``json=`` (httpx2 serializes and stamps the
    content-type), and form bodies pass them to ``data=`` (httpx2 urlencodes).
    """
    out = msgspec.to_builtins(value)
    return _drop_none(out) if drop_none else out
