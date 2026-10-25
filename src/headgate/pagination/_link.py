# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""The ``Link`` response header (RFC 8288), parsed: what :class:`~headgate.pagination.LinkHeader` follows.

The grammar is ``#( "<" URI-Reference ">" *( OWS ";" OWS link-param ) )`` with ``link-param = token BWS [ "=" BWS (
token / quoted-string ) ]``. A comma inside the ``<...>`` target or inside a quoted parameter value does not separate
links, which is why the header is scanned rather than split. The parser is lenient: a malformed link-value is skipped
up to the next comma that separates links, and it never raises.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Mapping

    import httpx2

__all__ = ["Link", "next_link", "parse_link_header"]

#: Optional whitespace (RFC 9110 ``OWS``).
_OWS: Final = " \t"

#: Characters that end an unquoted parameter name or value.
_PARAM_END: Final = " \t=;,"


@dataclass(frozen=True, slots=True)
class Link:
    """One link-value: its target as written (possibly relative) and its parameters.

    Parameter names are lower-cased; a parameter given twice keeps its first value, as RFC 8288 requires for ``rel``,
    and one given without a value maps to ``""``.
    """

    target: str
    params: Mapping[str, str]

    @property
    def rels(self) -> frozenset[str]:
        """Return the link's relation types, lower-cased (``rel`` may hold several, separated by spaces)."""
        return frozenset(self.params.get("rel", "").lower().split())


def parse_link_header(value: str) -> list[Link]:
    """Return the links in one ``Link`` header *value*, in order."""
    links: list[Link] = []
    i, end = 0, len(value)
    while i < end:
        i = _skip(value, i, _OWS + ",")
        if i >= end:
            break
        if value[i] != "<":
            i = _after_link(value, i)
            continue
        close = value.find(">", i + 1)
        if close == -1:
            break
        target = value[i + 1 : close]
        params, i = _params(value, close + 1)
        links.append(Link(target, MappingProxyType(params)))
    return links


def next_link(headers: httpx2.Headers) -> str | None:
    """Return the target of the first ``rel="next"`` link in *headers*' ``Link`` fields, or ``None``."""
    for value in headers.get_list("link"):
        for link in parse_link_header(value):
            if "next" in link.rels:
                return link.target
    return None


def _skip(value: str, i: int, chars: str) -> int:
    """Return the index of the first character at or after *i* that is not in *chars*."""
    while i < len(value) and value[i] in chars:
        i += 1
    return i


def _scan(value: str, i: int, stops: str) -> int:
    """Return the index of the first character at or after *i* that is in *stops*, or the end."""
    while i < len(value) and value[i] not in stops:
        i += 1
    return i


def _quoted(value: str, i: int) -> tuple[str, int]:
    """Read the quoted-string opening at *i*; return its unescaped text and the index after its closing quote."""
    out: list[str] = []
    j = i + 1
    while j < len(value):
        char = value[j]
        if char == '"':
            return "".join(out), j + 1
        if char == "\\" and j + 1 < len(value):
            j += 1
            char = value[j]
        out.append(char)
        j += 1
    return "".join(out), j


def _after_link(value: str, i: int) -> int:
    """Return the index after the next comma at or after *i* that separates links (one outside a quoted string)."""
    while i < len(value):
        if value[i] == ",":
            return i + 1
        if value[i] == '"':
            _, i = _quoted(value, i)
        else:
            i += 1
    return i


def _params(value: str, i: int) -> tuple[dict[str, str], int]:
    """Read the ``;``-separated parameters starting at *i*; return them and the index of the next link-value."""
    params: dict[str, str] = {}
    while True:
        i = _skip(value, i, _OWS)
        if i >= len(value) or value[i] != ";":
            break
        i = _skip(value, i + 1, _OWS)
        name_end = _scan(value, i, _PARAM_END)
        name = value[i:name_end].lower()
        i = _skip(value, name_end, _OWS)
        text = ""
        if i < len(value) and value[i] == "=":
            i = _skip(value, i + 1, _OWS)
            if i < len(value) and value[i] == '"':
                text, i = _quoted(value, i)
            else:
                text_end = _scan(value, i, _PARAM_END)
                text, i = value[i:text_end], text_end
        if name:
            params.setdefault(name, text)
    return params, _after_link(value, i)
