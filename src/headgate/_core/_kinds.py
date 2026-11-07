# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""The check that refuses an option value of the wrong kind, naming what it got, and how it writes a type."""

from __future__ import annotations

import re
from typing import Any, Final, NewType, TypeAliasType, get_origin

__all__ = ["check_kind", "type_text"]

#: In a type expression's ``repr``, a quoted string (group 1, kept) or a run of dotted prefixes (dropped): the module
#: and enclosing names before a class, ``<locals>`` included.
_QUALIFIERS: Final = re.compile(r"""('(?:[^'\\]|\\.)*'|"(?:[^"\\]|\\.)*")|(?<![\w.])(?:[A-Za-z_]\w*\.|<locals>\.)+""")


def _is_type_expression(value: object) -> bool:
    """Return whether *value* is a type expression other than a plain class: ``list[Order]``, an alias, ``Any``."""
    return get_origin(value) is not None or isinstance(value, TypeAliasType | NewType) or value is Any


def type_text(value: object) -> str:
    """Return the type expression or class *value* as written, without qualifiers: ``list[Order]``, ``Order``.

    A class is its ``__name__``. Any other expression is its ``repr`` with, outside quoted strings, every run of
    dotted prefixes dropped (module names, enclosing classes and functions, ``<locals>``), so ``Literal['a.b']`` keeps
    its string.
    """
    if isinstance(value, type) and not _is_type_expression(value):
        return value.__name__
    return _QUALIFIERS.sub(lambda match: match[1] or "", repr(value))


def _named(value: object) -> str:
    """Return what *value* is, for a refusal: ``the type list[Order]``, ``the class Order``, or its type's name."""
    if _is_type_expression(value):
        return f"the type {type_text(value)}"
    if isinstance(value, type):
        return f"the class {value.__name__}"
    return type(value).__name__


def check_kind(option: str, value: object, kind: type | tuple[type, ...], wanted: str) -> None:
    """Refuse a *value* for *option* that is not a *kind*, such as a bare number where a value object goes.

    Such a value would otherwise pass construction and fail at the first request, far from where it was written.

    :raises TypeError: naming the option, what it takes (*wanted*) and what it got: ``the type <expression>`` for a
        type expression (``list[Order]``, ``Order | None``, a ``type`` alias, a ``NewType``, ``Any``), ``the class
        <name>`` for a class, and its type's name for anything else.
    """
    if not isinstance(value, kind):
        msg = f"{option}= takes {wanted} (got {_named(value)})"
        raise TypeError(msg)
