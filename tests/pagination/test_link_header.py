# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""The RFC 8288 ``Link`` header parser: examples, then properties over any header a server could send."""

from __future__ import annotations

import httpx2
from hypothesis import given
from hypothesis import strategies as st

from headgate.pagination import Link, parse_link_header
from headgate.pagination._link import next_link


def _pairs(header: str) -> list[tuple[str, dict[str, str]]]:
    """Return *header*'s links as plain ``(target, params)`` pairs."""
    return [(link.target, dict(link.params)) for link in parse_link_header(header)]


def test_a_github_style_header_yields_each_link_with_its_relation() -> None:
    """Parse several comma-separated links, each with a quoted ``rel``."""
    header = '<https://api.test/items?page=2>; rel="next", <https://api.test/items?page=9>; rel="last"'
    assert _pairs(header) == [
        ("https://api.test/items?page=2", {"rel": "next"}),
        ("https://api.test/items?page=9", {"rel": "last"}),
    ]


def test_commas_and_semicolons_inside_targets_and_quoted_values_do_not_split() -> None:
    """Keep a comma or semicolon inside ``<...>`` or a quoted string within its link."""
    header = '<https://api.test/a,b;c>; title="x, y; z", <https://api.test/d>; rel=next'
    assert _pairs(header) == [
        ("https://api.test/a,b;c", {"title": "x, y; z"}),
        ("https://api.test/d", {"rel": "next"}),
    ]


def test_quoted_pairs_are_unescaped() -> None:
    """Unescape a backslash-quoted character in a quoted value."""
    assert _pairs(r'<u>; title="say \"hi\" \\ bye"') == [("u", {"title": 'say "hi" \\ bye'})]


def test_parameter_names_are_case_insensitive_and_the_first_occurrence_wins() -> None:
    """Lower-case parameter names, keep the first of a repeated one, and give a bare one an empty value."""
    assert _pairs("<u> ; REL = next ; rel=prev; hreflang ;") == [("u", {"rel": "next", "hreflang": ""})]


def test_rel_may_hold_several_relation_types() -> None:
    """Read every space-separated relation type in ``rel``, case-insensitively."""
    (link,) = parse_link_header('<u>; rel="Prev NEXT"')
    assert link.rels == frozenset({"prev", "next"})
    assert Link("u", {}).rels == frozenset()


def test_malformed_link_values_are_skipped() -> None:
    """Skip a value that does not open with ``<`` up to the next separating comma, and stop at an unclosed one."""
    assert _pairs('junk "with, comma", <u>; rel=next, <unclosed') == [("u", {"rel": "next"})]
    assert _pairs('<u>; rel=next garbage "x,y", <v>') == [("u", {"rel": "next"}), ("v", {})]
    assert _pairs('<u>; title="never closed') == [("u", {"title": "never closed"})]
    assert _pairs("<u>; a==b; =c") == [("u", {"a": ""})]
    assert _pairs(" , ,") == []


def test_the_next_link_is_found_across_several_link_fields() -> None:
    """Find ``rel="next"`` in any of the response's ``Link`` fields, the first one winning."""
    headers = httpx2.Headers([("link", '<a>; rel="prev"'), ("Link", '<b>; rel="next", <c>; rel="next"')])
    assert next_link(headers) == "b"
    assert next_link(httpx2.Headers({"link": '<a>; rel="last"'})) is None
    assert next_link(httpx2.Headers()) is None


#: A URI reference as a server writes one in ``<...>``: anything but ``>`` and the characters around it in the grammar.
_TARGETS = st.text(alphabet=st.characters(exclude_characters="<>", exclude_categories=["Cs", "Cc"]), max_size=40)
#: A parameter name: an RFC 9110 token, lower-cased since the parser lower-cases names.
_NAMES = st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789!#$%&'*+-.^_`|~", min_size=1, max_size=10)
#: A parameter value: any text, sent as a quoted string.
_VALUES = st.text(alphabet=st.characters(exclude_categories=["Cs", "Cc"]), max_size=20)
_LINKS = st.lists(st.tuples(_TARGETS, st.dictionaries(_NAMES, _VALUES, max_size=4)), max_size=5)


def _quote(value: str) -> str:
    """Return *value* as an RFC 9110 quoted-string."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _render(links: list[tuple[str, dict[str, str]]]) -> str:
    """Write *links* as one ``Link`` header value."""
    return ", ".join(
        f"<{target}>" + "".join(f"; {name}={_quote(value)}" for name, value in params.items())
        for target, params in links
    )


@given(_LINKS)
def test_any_well_formed_header_round_trips(links: list[tuple[str, dict[str, str]]]) -> None:
    """Parse any header written by the grammar back into exactly the links and parameters it was written from."""
    assert _pairs(_render(links)) == links


@given(st.text())
def test_parsing_never_raises_and_every_target_comes_from_the_header(header: str) -> None:
    """Parse arbitrary text without raising, returning only targets that occur in it."""
    for link in parse_link_header(header):
        assert f"<{link.target}>" in header
