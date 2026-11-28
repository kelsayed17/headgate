# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""The implementation of :mod:`headgate.pydantic`, which exports it and documents it."""

import functools
import re
from typing import Any, ClassVar, Final, cast, overload

try:
    from pydantic import BaseModel, PydanticSchemaGenerationError, RootModel, TypeAdapter, ValidationError
    from pydantic_core import PydanticKnownError, to_json, to_jsonable_python
except ImportError as exc:
    msg = "headgate.pydantic needs pydantic: pip install headgate[pydantic]"
    raise ImportError(msg) from exc

from collections.abc import Callable, Mapping

from pydantic_core import ErrorDetails
from pydantic_core.core_schema import ErrorType
from typing_extensions import TypeForm

from headgate._codec import decode_with, reader_for, writer_for
from headgate._codec import drop_none as _drop_none
from headgate._core import type_text
from headgate._failures import NESTED_TOO_DEEPLY
from headgate._request_body import Writer
from headgate._responses import Reader
from headgate.errors import RefusedError

__all__ = ["CODEC", "PydanticCodec", "decode", "reader", "to_builtins", "writer"]

#: The class pydantic names when it has no schema for one, as ``<class 'module.Name'>``.
_SCHEMALESS: Final = re.compile(r"<class '([^']+)'>")

#: How many of a validation's failures a ``DecodeError`` message names; the count of the rest follows them.
_ERRORS_SHOWN = 5

#: What a message shows in place of the input: a key in a failure's location, or a value its reason would quote.
_REDACTED: Final = "[...]"

#: What pydantic's ``json_invalid`` reason holds for a body nested past its parser's recursion limit.
_RECURSION_LIMIT: Final = "recursion limit exceeded"

#: The failures whose reason quotes the input, each with the context value that holds it: an unknown tag, the character
#: a UUID failed at, and a timezone offset the input named.
_QUOTING: Final = {"union_tag_invalid": "tag", "uuid_parsing": "error", "timezone_offset": "tz_actual"}

#: Every name the schemas this module compiled hold: field names and aliases, tags, and model and type names. A part of
#: a failure's location is shown only if it is one of them, so a key of the input's own never is. ``[key]`` is the part
#: pydantic adds for a failure in a dict's key rather than its value.
_NAMES: Final = {"[key]"}


def _schema_names(schema: object) -> set[str]:
    """Return every string *schema*, a pydantic core schema, holds as a key or a value, however deep."""
    names: set[str] = set()
    pending: list[object] = [schema]
    seen: set[int] = set()
    while pending:
        node = pending.pop()
        if isinstance(node, str):
            names.add(node)
        elif isinstance(node, dict | list | tuple) and id(node) not in seen:
            seen.add(id(node))
            pending.extend([*node.keys(), *node.values()] if isinstance(node, dict) else node)
    return names


@functools.cache
def _cached_adapter(type_: TypeForm[object]) -> TypeAdapter[Any]:
    """Return the one ``TypeAdapter`` for *type_*, built on first use, adding the names its schema holds to ``_NAMES``.

    A codec's ``decoder`` typically runs on every request, and :func:`decode` may run once per element of a larger
    document, so each type expression is compiled once. A cache shared by every type expression cannot carry each
    entry's type parameter, which is why it returns ``TypeAdapter[Any]``; :func:`_adapter` restores it.
    """
    adapter = TypeAdapter(type_)
    _NAMES.update(_schema_names(adapter.core_schema))
    return adapter


def _adapter[T](type_: TypeForm[T]) -> TypeAdapter[T]:
    """Return the cached ``TypeAdapter`` for the type expression *type_*, typed by it.

    :raises TypeError: for a type that holds a class pydantic has no schema for (a plain class, a msgspec struct),
        naming the class, as the msgspec flavor refuses a class it does not know.
    """
    try:
        # The cache key is the type expression itself, so the entry it returns was built from exactly ``TypeForm[T]``.
        return cast("TypeAdapter[T]", _cached_adapter(type_))
    except PydanticSchemaGenerationError as exc:
        found = _SCHEMALESS.search(str(exc))
        name = type_text(type_) if found is None else found[1].rpartition(".")[2]
        msg = (
            f"headgate.pydantic cannot decode {name}, a class pydantic has no schema for: declare it as a pydantic "
            "model, a dataclass or a TypedDict, and read a msgspec.Struct with headgate.msgspec.reader"
        )
        raise TypeError(msg) from None


class PydanticCodec:
    """JSON through pydantic, as a :class:`~headgate.Codec`: each type expression's cached ``TypeAdapter``.

    *strict* (the default) rejects pydantic's lax coercions, so a JSON string where an ``int`` is declared is wire
    drift. Field aliases are the wire's names both ways. *drop_none* leaves ``None``-valued fields out of what it
    encodes, recursively, as :func:`to_builtins` does.
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
        """Return a function serializing a value by *type_*'s cached adapter, by alias.

        With *drop_none*, ``None`` goes from nested dicts too, as :func:`to_builtins` drops it (``exclude_none`` would
        keep a ``dict[str, int | None]``'s), so the bytes match the msgspec flavor's.
        """
        adapter = _adapter(type_)
        if not self._drop_none:
            return functools.partial(adapter.dump_json, by_alias=True)

        def encode(value: T) -> bytes:
            return to_json(_drop_none(adapter.dump_python(value, mode="json", by_alias=True)))

        return encode

    def describe(self, error: Exception, /) -> str:
        """Summarize a ``ValidationError`` without any of the input: its first failures, and how many there were.

        A failure's location keeps list positions and the names the type declares (fields and aliases, tags), and shows
        ``[...]`` for a key of the input's own; a reason that would quote the input (an unknown tag) shows ``[...]`` in
        its place. A validator of your own is quoted as it words its message. A body nested past pydantic's recursion
        limit is ``nested too deeply``, as every codec says it.
        """
        exc = cast("ValidationError", error)  # the only one of :attr:`errors`
        errors = exc.errors(include_input=False, include_url=False, include_context=True)[:_ERRORS_SHOWN]
        if any(error["type"] == "json_invalid" and _RECURSION_LIMIT in error["msg"] for error in errors):
            return NESTED_TOO_DEEPLY
        shown = "; ".join(f"{_location(error['loc'])}: {_reason(error)}" for error in errors)
        count = exc.error_count()
        more = f" (+{count - len(errors)} more)" if count > len(errors) else ""
        noun = "error" if count == 1 else "errors"
        return f"{count} validation {noun} for {exc.title}: {shown}{more}"


def _location(loc: tuple[int | str, ...]) -> str:
    """Return *loc* dotted, with ``[...]`` for each part that is neither a list position nor a name a schema holds."""
    return ".".join(str(part) if isinstance(part, int) or part in _NAMES else _REDACTED for part in loc) or "<body>"


def _reason(error: ErrorDetails) -> str:
    """Return *error*'s message, rendered again with ``[...]`` for the input it would quote."""
    quoted = _QUOTING.get(error["type"])
    context = error.get("ctx")
    if quoted is None or context is None or quoted not in context:
        return error["msg"]
    kind = cast("ErrorType", error["type"])  # one of :data:`_QUOTING`'s keys, each a pydantic error type
    return PydanticKnownError(kind, {**context, quoted: _REDACTED}).message()


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


def reader[T](
    type_: TypeForm[T],
    *,
    strict: bool = True,
    allow_empty_body: bool = False,
    refuse: Callable[[T], RefusedError | None] | None = None,
) -> Reader[T]:
    """Build a reader that validates the body as strict JSON against the type expression *type_*.

    Accepts the same shapes as :func:`headgate.msgspec.reader` (an enveloped model, ``list[Row]``,
    ``dict[str, Row]``, a union of shapes). One malformed element fails the whole body. ``allow_empty_body`` and
    ``refuse`` are documented on :func:`~headgate.reader_for`.
    """
    codec = CODEC if strict else _LAX
    return reader_for(codec, type_, allow_empty_body=allow_empty_body, refuse=refuse)


def writer[T](type_: TypeForm[T], *, drop_none: bool = False) -> Writer[T]:
    """Build a writer that encodes a value of the type expression *type_* as a JSON request body.

    The mirror of :func:`reader`: build it once, at import (``NEW_ORDER = writer(NewOrder)``), and send with
    ``body=NEW_ORDER(order)``. The value is serialized by *type_*'s ``TypeAdapter``, with field aliases applied, as
    :func:`to_builtins` applies them. ``drop_none=True`` removes ``None``-valued fields recursively, as
    :func:`to_builtins` does.
    """
    return writer_for(_DROPPING_NONE if drop_none else CODEC, type_)


@overload
def to_builtins(value: RootModel[Any], *, drop_none: bool = ...) -> object: ...
@overload
def to_builtins(value: BaseModel | Mapping[str, object], *, drop_none: bool = ...) -> dict[str, object]: ...
@overload
def to_builtins[T](value: list[T], *, drop_none: bool = ...) -> list[object]: ...
@overload
def to_builtins(value: object, *, drop_none: bool = ...) -> object: ...
def to_builtins(value: object, *, drop_none: bool = False) -> object:
    """Convert a model, or a list or mapping of them, to builtins ready for ``body=Json(...)`` or ``Form(...)``.

    Field aliases are applied (``by_alias=True``), so a model declares the wire's names once. A ``RootModel``
    converts to whatever its root converts to. ``drop_none=True`` also removes ``None``-valued fields recursively,
    keeping ``False`` and ``0``.
    """
    out = to_jsonable_python(value, by_alias=True)
    return _drop_none(out) if drop_none else out
