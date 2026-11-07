# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""``check_kind``'s naming of what it got: a type expression as written, without the module names around it."""

from __future__ import annotations

import enum
from typing import Annotated, Literal, NewType

import pytest

from headgate._core import check_kind, type_text


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


def test_a_refusal_names_a_type_expression_as_a_type() -> None:
    """Name a ``NewType`` as a type, not by its own class."""
    with pytest.raises(TypeError, match=r"^x= takes a thing \(got the type UserId\)$"):
        check_kind("x", UserId, Order, "a thing")
