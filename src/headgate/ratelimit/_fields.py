# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""A parser for the Structured Field Lists (RFC 9651) the IETF rate-limit fields are written in.

``RateLimit`` and ``RateLimit-Policy`` (draft-ietf-httpapi-ratelimit-headers) are Lists of Items, each a String naming a
policy with parameters such as ``r=0`` or ``q=100``. This parses that subset: a List of Items whose bare items are
Strings, Tokens, Integers, Decimals, Byte Sequences or Booleans, each with parameters. An Inner List, or anything the
grammar does not allow, makes the whole field malformed, and the draft says a malformed field is ignored.
"""

from __future__ import annotations

import base64
import binascii
import string
from dataclasses import dataclass, field
from typing import Final

__all__ = ["Item", "parse_list"]

type BareItem = str | int | float | bytes | bool
"""A Structured Field bare item; a Token is read as its text, like a String."""

_TOKEN_START: Final = frozenset(string.ascii_letters + "*")
_TOKEN_CHARS: Final = frozenset(string.ascii_letters + string.digits + ":/!#$%&'*+-.^_`|~")
_KEY_START: Final = frozenset(string.ascii_lowercase + "*")
_KEY_CHARS: Final = frozenset(string.ascii_lowercase + string.digits + "_-.*")
_DIGITS: Final = frozenset(string.digits)
_BASE64: Final = frozenset(string.ascii_letters + string.digits + "+/=")
_OWS: Final = frozenset(" \t")
#: RFC 9651's bounds: an Integer has at most 15 digits, a Decimal at most 12 before its point and 3 after.
_MAX_INTEGER_DIGITS: Final = 15
_MAX_DECIMAL_INTEGER_DIGITS: Final = 12
_MAX_FRACTION_DIGITS: Final = 3


class _MalformedError(ValueError):
    """The field does not follow the grammar."""


@dataclass(frozen=True, slots=True)
class Item:
    """One member of a List: its bare item and its parameters, in order."""

    value: BareItem
    params: dict[str, BareItem] = field(default_factory=dict)


class _Reader:
    """A cursor over a field value."""

    __slots__ = ("pos", "text")

    def __init__(self, text: str) -> None:
        """Read *text* from its start."""
        self.text = text
        self.pos = 0

    def peek(self) -> str:
        """Return the next character, or ``""`` at the end."""
        return self.text[self.pos : self.pos + 1]

    def take(self) -> str:
        """Consume and return the next character, or ``""`` at the end."""
        char = self.peek()
        self.pos += len(char)
        return char

    def take_while(self, allowed: frozenset[str]) -> str:
        """Consume and return the run of characters in *allowed*."""
        start = self.pos
        while self.peek() in allowed:
            self.pos += 1
        return self.text[start : self.pos]

    def expect(self, char: str) -> None:
        """Consume *char*, or fail."""
        if self.take() != char:
            raise _MalformedError


def parse_list(text: str) -> list[Item] | None:
    """Return the Items of the List *text*, or ``None`` when it is malformed (an empty field is an empty List)."""
    try:
        return _members(_Reader(text.strip(" ")))
    except _MalformedError:
        return None


def _members(reader: _Reader) -> list[Item]:
    """Read every member of a List."""
    items: list[Item] = []
    while reader.peek():
        items.append(_item(reader))
        reader.take_while(_OWS)
        if not reader.peek():
            break
        reader.expect(",")
        reader.take_while(_OWS)
        if not reader.peek():
            raise _MalformedError
    return items


def _item(reader: _Reader) -> Item:
    """Read one Item: a bare item, then its parameters."""
    value = _bare_item(reader)
    params: dict[str, BareItem] = {}
    while reader.peek() == ";":
        reader.take()
        reader.take_while(frozenset(" "))
        if reader.peek() not in _KEY_START:
            raise _MalformedError
        key = reader.take_while(_KEY_CHARS)
        params[key] = True
        if reader.peek() == "=":
            reader.take()
            params[key] = _bare_item(reader)
    return Item(value, params)


def _bare_item(reader: _Reader) -> BareItem:
    """Read one bare item."""
    char = reader.peek()
    if char == '"':
        return _string(reader)
    if char == ":":
        return _bytes(reader)
    if char == "?":
        reader.take()
        flag = reader.take()
        if flag not in {"0", "1"}:
            raise _MalformedError
        return flag == "1"
    if char == "-" or char in _DIGITS:
        return _number(reader)
    if char in _TOKEN_START:
        return reader.take_while(_TOKEN_CHARS)
    raise _MalformedError


def _string(reader: _Reader) -> str:
    """Read a String: printable ASCII in double quotes, where a backslash escapes a quote or a backslash."""
    reader.expect('"')
    chars: list[str] = []
    while True:
        char = reader.take()
        if char == '"':
            return "".join(chars)
        if char == "\\":
            char = reader.take()
            if char not in {'"', "\\"}:
                raise _MalformedError
        elif not char or not " " <= char <= "~":
            raise _MalformedError
        chars.append(char)


def _bytes(reader: _Reader) -> bytes:
    """Read a Byte Sequence: base64 between colons."""
    reader.expect(":")
    encoded = reader.take_while(_BASE64)
    reader.expect(":")
    try:
        return base64.b64decode(encoded, validate=True)
    except binascii.Error as exc:
        raise _MalformedError from exc


def _number(reader: _Reader) -> int | float:
    """Read an Integer or a Decimal, within RFC 9651's digit limits."""
    sign = -1 if reader.peek() == "-" else 1
    if sign < 0:
        reader.take()
    whole = reader.take_while(_DIGITS)
    if not whole:
        raise _MalformedError
    if reader.peek() != ".":
        if len(whole) > _MAX_INTEGER_DIGITS:
            raise _MalformedError
        return sign * int(whole)
    reader.take()
    fraction = reader.take_while(_DIGITS)
    if len(whole) > _MAX_DECIMAL_INTEGER_DIGITS or not 0 < len(fraction) <= _MAX_FRACTION_DIGITS:
        raise _MalformedError
    return sign * float(f"{whole}.{fraction}")
