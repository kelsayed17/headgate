# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""``check_kind``'s naming of what it got: a type expression as written, without the module names around it."""

from __future__ import annotations

import enum
import re
from typing import Annotated, Literal, NewType

import pytest

from headgate._core import check_kind, check_number, check_statuses, check_str_mapping, type_text


class Order:
    """A model class."""


class Color(enum.Enum):
    """An enum, whose member a ``Literal`` may name."""

    RED = 1


UserId = NewType("UserId", int)


def test_a_class_defined_in_a_function_reads_by_its_name() -> None:
    """Drop the enclosing function and its ``<locals>`` from a class in a type expression."""

    class Row:
        pass

    assert type_text(list[Row]) == "list[Row]"
    assert type_text(Row) == "Row"


@pytest.mark.parametrize(
    ("value", "text"),
    [
        (Order | None, "Order | None"),
        (Literal["a.b"], "Literal['a.b']"),
        (Literal['say "x.y"'], """Literal['say "x.y"']"""),
        (Annotated[float, 0.5], "Annotated[float, 0.5]"),
        (Literal[Color.RED], "Literal[<RED: 1>]"),
        (UserId, "UserId"),
    ],
    ids=["union", "string", "quoted", "number", "enum", "newtype"],
)
def test_a_type_expression_keeps_its_strings_and_numbers(value: object, text: str) -> None:
    """Drop dotted qualifiers outside quoted strings only, so a literal's text and a number survive."""
    assert type_text(value) == text


@pytest.mark.parametrize(
    ("option", "subject"),
    [("x", "x="), ("Route(fragment)", "Route(fragment)"), ("Shop.refresh_margin", "Shop.refresh_margin")],
    ids=["keyword", "positional", "attribute"],
)
def test_a_refusal_names_a_keyword_by_its_keyword_and_anything_else_as_written(option: str, subject: str) -> None:
    """Name a keyword ``x=``, and a positional parameter or a class attribute as its call or its class reads."""
    with pytest.raises(TypeError, match=rf"^{re.escape(subject)} takes a thing \(got int\)$"):
        check_kind(option, 5, Order, "a thing")


def test_a_refusal_names_a_type_expression_as_a_type() -> None:
    """Name a ``NewType`` as a type, not by its own class."""
    with pytest.raises(TypeError, match=r"^x= takes a thing \(got the type UserId\)$"):
        check_kind("x", UserId, Order, "a thing")


@pytest.mark.parametrize("value", [True, "5", None], ids=["bool", "str", "none"])
def test_a_number_is_an_int_or_a_float_and_never_a_bool(value: object) -> None:
    """Refuse a ``bool``, which ``isinstance`` takes for an ``int``, and anything not a number."""
    with pytest.raises(TypeError, match=rf"^attempts= takes a count \(got {type(value).__name__}\)$"):
        check_number("attempts", value, "a count")


@pytest.mark.parametrize("value", [3, 2.5])
def test_an_int_and_a_float_are_numbers(value: float) -> None:
    """Accept an ``int`` and a ``float``."""
    check_number("attempts", value, "a count")


@pytest.mark.parametrize(
    ("value", "got"),
    [([("a", "b")], "list"), ({1: "a"}, "the key 1, of type int"), ({"a": 1}, "int under 'a'")],
    ids=["pairs", "key", "value"],
)
def test_a_mapping_of_text_names_its_first_bad_entry(value: object, got: str) -> None:
    """Refuse pairs, a key that is not a ``str``, and a value that is not one, naming which."""
    with pytest.raises(TypeError, match=rf"^m= takes text \(got {got}\)$"):
        check_str_mapping("m", value, "text")


def test_a_mapping_of_any_values_checks_only_its_keys() -> None:
    """Leave the values to what reads them when told to."""
    check_str_mapping("m", {"a": 1}, "text", any_values=True)
    with pytest.raises(TypeError, match=r"the key 1"):
        check_str_mapping("m", {1: 1}, "text", any_values=True)


@pytest.mark.parametrize(("value", "got"), [({True}, "bool True"), (("500",), "str '500'")], ids=["bool", "str"])
def test_statuses_are_ints(value: object, got: str) -> None:
    """Refuse a ``bool`` and a numeric string among statuses, naming the entry."""
    with pytest.raises(TypeError, match=rf"^statuses= takes codes \(got {got} among them\)$"):
        check_statuses("statuses", value, "codes")


@pytest.mark.parametrize("status", [99, 600])
def test_statuses_lie_between_100_and_599(status: int) -> None:
    """Refuse a status no response has."""
    with pytest.raises(ValueError, match=rf"^statuses= takes HTTP statuses, 100 to 599 \(got {status}\)$"):
        check_statuses("statuses", (200, status), "codes")


def test_statuses_are_a_collection() -> None:
    """Refuse a bare ``str``, as every collection of codes does, and accept the edges of the range."""
    check_statuses("statuses", (100, 599), "codes")
    with pytest.raises(TypeError, match=r"^statuses= takes codes \(got str\)$"):
        check_statuses("statuses", "500", "codes")
