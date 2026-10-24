"""The pydantic codec flavor: strict JSON decode into ``pydantic`` models, and request-body preparation.

The same surface as :mod:`headgate.msgspec`, so switching flavor is changing one import:
``from headgate.pydantic import decoder, to_builtins``. Wire models are frozen, strict ``BaseModel``s::

    class Row(BaseModel):
        model_config = ConfigDict(strict=True, extra="ignore", frozen=True)

Strictness parity with the msgspec flavor is held by tests: a JSON string where an ``int`` is declared fails loud, an
unknown field is ignored, and every failure is the shared :class:`~headgate.errors.DecodeError`.

Requires the ``pydantic`` extra: ``pip install headgate[pydantic]``.
"""

from __future__ import annotations

import functools
from typing import TYPE_CHECKING, Any, overload

try:
    from pydantic import TypeAdapter, ValidationError
    from pydantic_core import to_jsonable_python
except ImportError as exc:  # pragma: no cover - exercised by the extras CI job
    msg = "headgate.pydantic needs pydantic: pip install headgate[pydantic]"
    raise ImportError(msg) from exc

from headgate._builtins import drop_none as _drop_none
from headgate._responses import json_reader
from headgate.errors import DecodeError

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import UnionType

    from headgate._responses import Reader, Refused

__all__ = ["decode", "decoder", "to_builtins"]


@functools.cache
def _adapter(type_: Any) -> TypeAdapter[Any]:
    """One ``TypeAdapter`` per type expression: building one is expensive, and ``decoder`` runs on every request."""
    return TypeAdapter(type_)


@overload
def decode[T](content: bytes, type_: type[T], *, strict: bool = ...) -> T: ...
@overload
def decode(content: bytes, type_: UnionType, *, strict: bool = ...) -> Any: ...  # a union pins no type
def decode[T](content: bytes, type_: type[T] | UnionType, *, strict: bool = True) -> T | Any:
    """Strict pydantic JSON validation of *content* into *type_*, raising :class:`DecodeError` on failure.

    ``strict=True`` rejects pydantic's lax coercions, so a JSON string where an ``int`` is declared is wire drift and
    fails loud. Invalid JSON and a type mismatch are the same error.
    """
    try:
        return _adapter(type_).validate_json(content, strict=strict)
    except ValidationError as exc:
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
    """A reader that validates the body as strict JSON against the type expression ``type_``.

    Accepts the same shapes as :func:`headgate.msgspec.decoder` (an enveloped model, ``list[Row]``, ``dict[str, Row]``,
    a union of shapes). One malformed element fails the whole validation. ``allow_empty_body`` and ``check`` are
    documented on :func:`~headgate._responses.json_reader`.
    """
    return json_reader(
        lambda content: decode(content, type_, strict=strict), allow_empty_body=allow_empty_body, check=check
    )


def to_builtins(value: object, *, drop_none: bool = False) -> Any:  # JSON builtins for httpx2 json=/data=
    """Convert a pydantic model (or a sequence or mapping of them) to ``json=``-ready builtins.

    Field aliases are applied (``by_alias=True``), so a model declares the wire's names once. ``drop_none=True``
    additionally removes ``None``-valued fields recursively, keeping ``False`` and ``0``.
    """
    out = to_jsonable_python(value, by_alias=True)
    return _drop_none(out) if drop_none else out
