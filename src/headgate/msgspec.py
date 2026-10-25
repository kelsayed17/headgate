# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""The msgspec codec flavor: strict JSON decoding into ``msgspec.Struct`` types, and request-body preparation.

A client picks its flavor by import (``from headgate.msgspec import decoder, to_builtins``), and the base client stays
an HTTP-policy layer with no serialization knowledge of its own. :func:`decode` is the bytes-level primitive,
:func:`decoder` builds a response reader on it, and :func:`to_builtins` prepares a request body. Every decode failure
is the shared :class:`~headgate.errors.DecodeError`.

Drift detection is strict **typing**, not unknown-field rejection. ``strict=True`` catches a field that changes type
on the wire, which silently corrupts values. ``forbid_unknown_fields`` would catch a field appearing, which providers
do routinely and without notice: rejecting one turns a harmless upstream addition into a failure on every call, while
ignoring it costs nothing, since a field nobody models is a field nobody reads. A struct can still opt in with
``forbid_unknown_fields=True`` where noticing a new field is worth that risk.

``type_`` parameters are annotated ``TypeForm[T]`` (PEP 747), so a union or a generic alias such as ``list[A] | A``
types the result as exactly that expression.
"""

from __future__ import annotations

import functools
from typing import TYPE_CHECKING, overload

import msgspec

from headgate._codec import drop_none as _drop_none
from headgate._responses import json_reader
from headgate.errors import DecodeError

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from typing_extensions import TypeForm

    from headgate._responses import Reader, RefusedError

__all__ = ["decode", "decoder", "to_builtins"]


def _json_decoder[T](type_: TypeForm[T], *, strict: bool) -> msgspec.json.Decoder[T]:
    """Build a ``msgspec.json.Decoder`` for *type_*.

    msgspec's stubs type a non-``type[T]`` argument as ``Any``; the annotation records that the decoder decodes into
    exactly the type expression it was built from.
    """
    json_decoder: msgspec.json.Decoder[T] = msgspec.json.Decoder(type_, strict=strict)
    return json_decoder


def _decode_with[T](json_decoder: msgspec.json.Decoder[T], content: bytes) -> T:
    """Decode *content* with *json_decoder*, raising :class:`DecodeError` for malformed or mistyped JSON."""
    try:
        return json_decoder.decode(content)
    # msgspec.ValidationError subclasses msgspec.DecodeError, so the base covers malformed and mistyped bodies alike.
    except msgspec.DecodeError as exc:
        raise DecodeError(str(exc)) from exc


def decode[T](content: bytes, type_: TypeForm[T], *, strict: bool = True) -> T:
    """Decode *content* as strict JSON into the type expression *type_*.

    The primitive every JSON reader shares, and the one to call for bytes that are not a whole response: a document
    nested inside another, or a fixture read from disk. ``strict=True`` rejects msgspec's built-in coercions, so a JSON
    string where an ``int`` is declared is wire drift.

    :raises DecodeError: if *content* is not JSON or does not match *type_*.
    """
    return _decode_with(_json_decoder(type_, strict=strict), content)


def decoder[T](
    type_: TypeForm[T],
    *,
    strict: bool = True,
    allow_empty_body: bool = False,
    check: Callable[[T], RefusedError | None] | None = None,
) -> Reader[T]:
    """Build a reader that decodes the body as strict JSON into the type expression *type_*.

    *type_* carries the whole wire shape, so one function serves every JSON body: an enveloped object
    (``decoder(MyResponse)``), a bare array (``decoder(list[MyRow])``), a bare map (``decoder(dict[str, MyRow])``), or
    a union where a provider's body is genuinely one shape or the other (``decoder(list[A] | A)``).

    msgspec decodes the expression atomically, so one malformed element fails the whole body with
    :class:`~headgate.errors.DecodeError`. ``strict=False`` is the deliberate opt-out for a wire that needs lax
    coercion. ``allow_empty_body`` and ``check`` are documented on :func:`~headgate._responses.json_reader`.
    """
    decode_body = functools.partial(_decode_with, _json_decoder(type_, strict=strict))
    return json_reader(decode_body, allow_empty_body=allow_empty_body, check=check)


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
