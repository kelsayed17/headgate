# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""The check that refuses an option value of the wrong kind, naming what it got, and how it writes a type."""

import re
from collections.abc import Callable, Collection, Mapping
from typing import Any, Final, NewType, NoReturn, TypeAliasType, cast, get_origin

import httpx2

__all__ = [
    "check_callable",
    "check_collection",
    "check_default_headers",
    "check_header_name",
    "check_header_value",
    "check_headers",
    "check_int",
    "check_kind",
    "check_names",
    "check_not_letters",
    "check_number",
    "check_options",
    "check_status",
    "check_statuses",
    "check_str_mapping",
    "is_type",
    "named",
    "option_text",
    "refuse_pickle",
    "refuse_subclass",
    "sealed",
    "type_text",
]

#: In a type expression's ``repr``, a quoted string (group 1, kept) or a run of dotted prefixes (dropped): the module
#: and enclosing names before a class, ``<locals>`` included.
_QUALIFIERS: Final = re.compile(r"""('(?:[^'\\]|\\.)*'|"(?:[^"\\]|\\.)*")|(?<![\w.])(?:[A-Za-z_]\w*\.|<locals>\.)+""")


def _is_type_expression(value: object) -> bool:
    """Return whether *value* is a type expression other than a plain class: ``list[Order]``, an alias, ``Any``."""
    return get_origin(value) is not None or isinstance(value, TypeAliasType | NewType) or value is Any


def is_type(value: object) -> bool:
    """Return whether *value* is a class or a type expression: a model given where its reader or writer goes."""
    return isinstance(value, type) or _is_type_expression(value)


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


def option_text(option: str) -> str:
    """Return how a refusal names *option*: a keyword as ``name=``, and anything else as written.

    A parameter a caller cannot write as a keyword is given as its call reads, ``Route(fragment)`` or
    ``reply(*replies)``, since ``fragment=`` would name a keyword the call does not take; a class attribute as
    ``Shop.refresh_margin``.
    """
    return f"{option}=" if option.isidentifier() else option


def refuse_subclass(cls: type, base: type, instead: str) -> NoReturn:
    """Refuse *cls*, a subclass of the final *base*, whose overrides nothing would call, saying what to write *instead*.

    For a value the stack reads by its fields: a method a subclass overrode would be accepted and never run, as ty
    reports a subclass of a ``@final`` class.

    :raises TypeError: always.
    """
    msg = f"{cls.__name__} cannot subclass {base.__name__}, which is final: {instead}"
    raise TypeError(msg)


#: The hook Python runs on a class's bases as a subclass of them is defined.
_SUBCLASS_HOOK: Final = "__init_subclass__"


def sealed[C: type](instead: str) -> Callable[[C], C]:
    """Return a class decorator that makes its class final at runtime, as ``typing.final`` above it does to ty.

    Defining a subclass of the class raises :func:`refuse_subclass`'s ``TypeError``, saying what to write *instead*.
    For a class headgate reads by its fields or holds as a handle, whose overrides nothing would call. It sets the
    class's ``__init_subclass__``, so it goes on a class that defines none, above ``@dataclass``, which may rebuild it.
    """

    def seal(cls: C) -> C:
        def refuse(subclass: type, **kwargs: object) -> None:
            del kwargs
            refuse_subclass(subclass, cls, instead)

        setattr(cls, _SUBCLASS_HOOK, classmethod(refuse))
        return cls

    return seal


def refuse_pickle(handle: object) -> NoReturn:
    """Refuse to pickle *handle*, live state its clients share within one process, which no other process could hold.

    :raises TypeError: always, naming the handle's class, where pickling would fail on the lock inside it.
    """
    msg = (
        f"a {type(handle).__name__} is live state its clients share within one process, so it cannot be pickled: "
        "build one in each process that uses it"
    )
    raise TypeError(msg)


def check_kind(option: str, value: object, kind: type | tuple[type, ...], wanted: str) -> None:
    """Refuse a *value* for *option* that is not a *kind*, such as a bare number where a value object goes.

    Such a value would otherwise pass construction and fail at the first request, far from where it was written.

    :raises TypeError: naming the option, what it takes (*wanted*) and what it got: ``the type <expression>`` for a
        type expression (``list[Order]``, ``Order | None``, a ``type`` alias, a ``NewType``, ``Any``), ``the class
        <name>`` for a class, and its type's name for anything else.
    """
    if not isinstance(value, kind):
        msg = f"{option_text(option)} takes {wanted} (got {named(value)})"
        raise TypeError(msg)


def check_int(option: str, value: object, wanted: str) -> None:
    """Refuse a *value* for *option* that is not an ``int``, a ``bool`` included, which ``isinstance`` takes for one.

    :raises TypeError: naming the option, what it takes (*wanted*) and what it got.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        msg = f"{option_text(option)} takes {wanted} (got {named(value)})"
        raise TypeError(msg)


def check_number(option: str, value: object, wanted: str) -> None:
    """Refuse a *value* for *option* that is not an ``int`` or a ``float``, a ``bool`` included.

    A ``bool`` is an ``int`` to ``isinstance`` and to a type checker, so ``True`` would pass for one second or one
    request.

    :raises TypeError: naming the option, what it takes (*wanted*) and what it got.
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        msg = f"{option_text(option)} takes {wanted} (got {named(value)})"
        raise TypeError(msg)


def check_str_mapping(option: str, value: object, wanted: str, *, any_values: bool = False) -> None:
    """Refuse a *value* for *option* that is not a mapping of ``str`` to ``str``: a list of pairs, above all.

    With *any_values*, only the keys must be ``str``: the values are checked by what reads them.

    :raises TypeError: naming the option, what it takes (*wanted*) and what it got: the value's type, a key that is not
        a ``str``, or a value that is not one under its key.
    """
    got = named(value) if not isinstance(value, Mapping) else _bad_entry(value, any_values=any_values)
    if got is not None:
        msg = f"{option_text(option)} takes {wanted} (got {got})"
        raise TypeError(msg)


def _bad_entry(mapping: Mapping[object, object], *, any_values: bool) -> str | None:
    """Return the first entry of *mapping* that is not ``str`` to ``str``, described, or ``None`` when all are."""
    for key, value in mapping.items():
        if not isinstance(key, str):
            return f"the key {key!r}, of type {type(key).__name__}"
        if not (any_values or isinstance(value, str)):
            return f"{type(value).__name__} under {key!r}"
    return None


#: An HTTP token (RFC 9110, section 5.6.2), which a header's name is.
_TOKEN: Final = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+")

#: What no header value may hold: a line break ends the header and starts another, and NUL ends it in C.
_LINE_BREAK: Final = re.compile("[\r\n\0]")


def check_header_name(option: str, name: str) -> None:
    """Refuse a header *name*, given in *option*, that is not an HTTP token: a space, a colon or a line break in it.

    :raises ValueError: naming the option and the name.
    """
    if not _TOKEN.fullmatch(name):
        msg = (
            f"{option_text(option)} takes header names that are HTTP tokens, of letters, digits and "
            f"!#$%&'*+-.^_`|~ (got {name!r})"
        )
        raise ValueError(msg)


def check_headers(option: str, value: object) -> None:
    """Refuse *value*, given as the headers *option*, unless it maps header names to ``str`` values a request can carry.

    A transport that writes each header as given (curl does) would end a header at a line break in its value or name,
    and send what follows as a header the caller never wrote.

    :raises TypeError: as :func:`check_str_mapping` does: for pairs, and a name or value that is not a ``str``.
    :raises ValueError: for a name that is not an HTTP token, and a value holding CR, LF or NUL, naming the header but
        not the value, which may be a secret.
    """
    check_str_mapping(option, value, "a mapping of header names to str values, such as {'Accept-Language': 'en'}")
    for name, text in cast("Mapping[str, str]", value).items():
        check_header_name(option, name)
        check_header_value(option, text, name)


def check_header_value(option: str, value: str, header: str) -> None:
    """Refuse *value*, sent as the header *header* by *option*, when it holds CR, LF or NUL, which no header can carry.

    :raises ValueError: naming the option and the header but not the value, which may be a secret: a line break would
        end the header there, and send what follows as a header the caller never wrote.
    """
    if _LINE_BREAK.search(value):
        msg = (
            f"{option_text(option)} takes header values with no CR, LF or NUL (got one under {header!r}): a line "
            "break would end the header there, and send the rest as another"
        )
        raise ValueError(msg)


def check_default_headers(option: str, value: object) -> None:
    """Refuse *value*, given as the default headers *option*, unless it maps each header name, once, to a ``str``.

    Default headers are merged by name, a call's own replacing them, so a set of them names each header once. Merged,
    one named twice, by two lines of an ``httpx2.Headers`` or by keys that differ only in case, becomes one line of
    its values joined with ``", "``, or one of them alone: neither is what was written, and a server reads
    ``Cookie: a=1, b=2`` as the one cookie ``a``, since a cookie's pairs are joined with ``"; "``.

    :raises TypeError: as :func:`check_headers` does.
    :raises ValueError: as :func:`check_headers` does, and for a header named more than once, compared
        case-insensitively, naming it.
    """
    check_headers(option, value)
    lines = value.multi_items() if isinstance(value, httpx2.Headers) else cast("Mapping[str, str]", value).items()
    seen: set[str] = set()
    for name in (name.lower() for name, _ in lines):
        if name in seen:
            msg = (
                f"{option_text(option)} takes each header once (got {name!r} more than once): give it once, its "
                "values joined in one string, with '; ' between a cookie's pairs and ', ' between a list header's "
                "values"
            )
            raise ValueError(msg)
        seen.add(name)


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
            msg = f"{option_text(option)} takes {wanted} (got {named(status)} {status!r} among them)"
            raise TypeError(msg)
        if status not in _STATUSES:
            msg = f"{option_text(option)} takes HTTP statuses, 100 to 599 (got {status})"
            raise ValueError(msg)


def check_status(option: str, value: object, wanted: str) -> None:
    """Refuse a *value* for *option* that is not one HTTP status: an ``int`` from 100 to 599.

    :raises TypeError: for a value that is not an ``int``, a ``bool`` included, naming the option and *wanted*.
    :raises ValueError: for an ``int`` outside 100-599, which no response has.
    """
    check_int(option, value, wanted)
    if value not in _STATUSES:
        msg = f"{option_text(option)} takes an HTTP status, 100 to 599 (got {value})"
        raise ValueError(msg)


def check_callable(option: str, value: object, wanted: str) -> None:
    """Refuse a *value* for *option* that cannot be called, such as a string where a function goes.

    :raises TypeError: naming the option, what it takes (*wanted*) and what it got.
    """
    if not callable(value):
        msg = f"{option_text(option)} takes {wanted} (got {named(value)})"
        raise TypeError(msg)


def check_collection(option: str, value: object, wanted: str) -> None:
    """Refuse a *value* for *option* that is not a collection: a bare ``str`` above all, and a one-shot iterator.

    A ``str`` would be read one character at a time, and reading an iterator to check it would empty it.

    :raises TypeError: naming the option, what it takes (*wanted*) and what it got.
    """
    if isinstance(value, str | bytes) or not isinstance(value, Collection):
        msg = f"{option_text(option)} takes {wanted} (got {named(value)})"
        raise TypeError(msg)


def check_names(option: str, value: object, wanted: str, *, kind: type | tuple[type, ...] = str) -> None:
    """Refuse a *value* for *option* that is not a collection of names (``str``, or *kind*), a bare ``str`` above all.

    A ``str`` would be read as one name per character, and reading an iterator to check it would empty it.

    :raises TypeError: naming the option, what it takes (*wanted*) and what it got: the value, or an entry that is not
        a *kind*.
    """
    check_collection(option, value, wanted)
    for name in cast("Collection[object]", value):
        if not isinstance(name, kind):
            msg = f"{option_text(option)} takes {wanted} (got {named(name)} {name!r} among them)"
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
