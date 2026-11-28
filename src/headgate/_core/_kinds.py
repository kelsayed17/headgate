# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""The check that refuses an option value of the wrong kind, naming what it got, and how it writes a type."""

import re
from collections.abc import Collection, Mapping
from typing import Any, Final, NewType, NoReturn, TypeAliasType, cast, get_origin

__all__ = [
    "check_callable",
    "check_collection",
    "check_headers",
    "check_int",
    "check_kind",
    "check_names",
    "check_not_letters",
    "check_number",
    "check_options",
    "check_statuses",
    "check_str_mapping",
    "named",
    "refuse_subclass",
    "type_text",
]

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


def named(value: object) -> str:
    """Return what *value* is, for a refusal: ``the type list[Order]``, ``the class Order``, or its type's name."""
    if _is_type_expression(value):
        return f"the type {type_text(value)}"
    if isinstance(value, type):
        return f"the class {value.__name__}"
    return type(value).__name__


def refuse_subclass(cls: type, base: type, instead: str) -> NoReturn:
    """Refuse *cls*, a subclass of the final *base*, whose overrides nothing would call, saying what to write *instead*.

    For a value the stack reads by its fields: a method a subclass overrode would be accepted and never run, as ty
    reports a subclass of a ``@final`` class.

    :raises TypeError: always.
    """
    msg = f"{cls.__name__} cannot subclass {base.__name__}, which is final: {instead}"
    raise TypeError(msg)


def check_kind(option: str, value: object, kind: type | tuple[type, ...], wanted: str) -> None:
    """Refuse a *value* for *option* that is not a *kind*, such as a bare number where a value object goes.

    Such a value would otherwise pass construction and fail at the first request, far from where it was written.

    :raises TypeError: naming the option, what it takes (*wanted*) and what it got: ``the type <expression>`` for a
        type expression (``list[Order]``, ``Order | None``, a ``type`` alias, a ``NewType``, ``Any``), ``the class
        <name>`` for a class, and its type's name for anything else.
    """
    if not isinstance(value, kind):
        msg = f"{option}= takes {wanted} (got {named(value)})"
        raise TypeError(msg)


def check_int(option: str, value: object, wanted: str) -> None:
    """Refuse a *value* for *option* that is not an ``int``, a ``bool`` included, which ``isinstance`` takes for one.

    :raises TypeError: naming the option, what it takes (*wanted*) and what it got.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        msg = f"{option}= takes {wanted} (got {named(value)})"
        raise TypeError(msg)


def check_number(option: str, value: object, wanted: str) -> None:
    """Refuse a *value* for *option* that is not an ``int`` or a ``float``, a ``bool`` included.

    A ``bool`` is an ``int`` to ``isinstance`` and to a type checker, so ``True`` would pass for one second or one
    request.

    :raises TypeError: naming the option, what it takes (*wanted*) and what it got.
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        msg = f"{option}= takes {wanted} (got {named(value)})"
        raise TypeError(msg)


def check_str_mapping(option: str, value: object, wanted: str, *, any_values: bool = False) -> None:
    """Refuse a *value* for *option* that is not a mapping of ``str`` to ``str``: a list of pairs, above all.

    With *any_values*, only the keys must be ``str``: the values are checked by what reads them.

    :raises TypeError: naming the option, what it takes (*wanted*) and what it got: the value's type, a key that is not
        a ``str``, or a value that is not one under its key.
    """
    got = named(value) if not isinstance(value, Mapping) else _bad_entry(value, any_values=any_values)
    if got is not None:
        msg = f"{option}= takes {wanted} (got {got})"
        raise TypeError(msg)


def _bad_entry(mapping: Mapping[object, object], *, any_values: bool) -> str | None:
    """Return the first entry of *mapping* that is not ``str`` to ``str``, described, or ``None`` when all are."""
    for key, value in mapping.items():
        if not isinstance(key, str):
            return f"the key {key!r}, of type {type(key).__name__}"
        if not (any_values or isinstance(value, str)):
            return f"{type(value).__name__} under {key!r}"
    return None


def check_headers(option: str, value: object) -> None:
    """Refuse *value*, given as the headers *option*, unless it maps header names to ``str`` values.

    :raises TypeError: as :func:`check_str_mapping` does: for pairs, and a name or value that is not a ``str``.
    """
    check_str_mapping(option, value, "a mapping of header names to str values, such as {'Accept-Language': 'en'}")


#: The status range RFC 9110 defines.
_STATUSES: Final = range(100, 600)


def check_statuses(option: str, value: object, wanted: str) -> None:
    """Refuse a *value* for *option* that is not a collection of HTTP statuses: ``int`` s from 100 to 599.

    :raises TypeError: for a value that is not a collection (a bare ``str``, a one-shot iterator), or holds anything
        but an ``int`` (a ``bool``, or ``"500"``, which no response's status equals), naming the option and the entry.
    :raises ValueError: for an ``int`` outside 100-599, which no response has.
    """
    check_collection(option, value, wanted)
    for status in cast("Collection[object]", value):
        if isinstance(status, bool) or not isinstance(status, int):
            msg = f"{option}= takes {wanted} (got {named(status)} {status!r} among them)"
            raise TypeError(msg)
        if status not in _STATUSES:
            msg = f"{option}= takes HTTP statuses, 100 to 599 (got {status})"
            raise ValueError(msg)


def check_callable(option: str, value: object, wanted: str) -> None:
    """Refuse a *value* for *option* that cannot be called, such as a string where a function goes.

    :raises TypeError: naming the option, what it takes (*wanted*) and what it got.
    """
    if not callable(value):
        msg = f"{option}= takes {wanted} (got {named(value)})"
        raise TypeError(msg)


def check_collection(option: str, value: object, wanted: str) -> None:
    """Refuse a *value* for *option* that is not a collection: a bare ``str`` above all, and a one-shot iterator.

    A ``str`` would be read one character at a time, and reading an iterator to check it would empty it.

    :raises TypeError: naming the option, what it takes (*wanted*) and what it got.
    """
    if isinstance(value, str | bytes) or not isinstance(value, Collection):
        msg = f"{option}= takes {wanted} (got {named(value)})"
        raise TypeError(msg)


def check_names(option: str, value: object, wanted: str) -> None:
    """Refuse a *value* for *option* that is not a collection of names, a bare ``str`` or a one-shot iterator above all.

    A ``str`` would be read as one name per character, and reading an iterator to check it would empty it.

    :raises TypeError: naming the option, what it takes (*wanted*) and what it got.
    """
    if (
        isinstance(value, str | bytes)
        or not isinstance(value, Collection)
        or not all(isinstance(name, str) for name in value)
    ):
        msg = f"{option}= takes {wanted} (got {named(value)})"
        raise TypeError(msg)


def check_not_letters(where: str, names: Collection[str]) -> None:
    """Refuse credential *names* that are all one character long: one name split into its letters, or a lone letter.

    ``frozenset("X-Api-Key")`` is a ``frozenset[str]`` of eight letters, so it type-checks and withholds no real header.
    No real header or query credential is one character long, so a non-empty set of nothing else is that mistake, or
    a name cut short. *where* names a declaration; one that ends in ``params`` is shown a query parameter's spelling.

    :raises TypeError: naming *where*, the names, and the spelling to write.
    """
    if not (names and all(len(name) == 1 for name in names)):
        return
    example = "api_key" if where.rstrip("=)").endswith("params") else "X-Api-Key"
    if len(names) == 1:
        (name,) = names
        msg = (
            f"{where} names only {name!r}, one character long, which no header or query credential is: write the "
            f"whole name, such as frozenset({{{example!r}}})"
        )
    else:
        msg = (
            f"{where} holds only one-character names ({', '.join(sorted(names))}), as frozenset({example!r}) does, "
            f"which splits a name into its letters: write frozenset({{{example!r}}})"
        )
    raise TypeError(msg)


def check_options(
    where: str, options: Mapping[str, object], allowed: frozenset[str], hints: Mapping[str, str] | None = None
) -> None:
    """Refuse a keyword *where* does not take, as a function with that signature would.

    :raises TypeError: naming the first such keyword in sorted order, and what to write instead when *hints* says.
    """
    if unknown := options.keys() - allowed:
        name = min(unknown)
        msg = f"{where}() got an unexpected keyword argument {name!r}"
        if hints is not None and (hint := hints.get(name)) is not None:
            msg = f"{msg}: {hint}"
        raise TypeError(msg)
