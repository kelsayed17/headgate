# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""Unit tests for :mod:`headgate.msgspec`, the strict msgspec codec flavor.

``decoder`` builds a reader that decodes the body strictly (a coerced token is drift) and raises the shared
:class:`DecodeError` on any failure. ``to_builtins`` is the encode side, struct to ``json=``-ready builtins, and
``drop_none=True`` recursively removes ``None``-valued fields.
"""

from __future__ import annotations

import enum

import msgspec
import pytest

from headgate._failures import MESSAGE_TEXT_LIMIT
from headgate.errors import DecodeError
from headgate.msgspec import MsgspecCodec, decode, decoder, to_builtins
from tests.support.responses import response


class _Struct(msgspec.Struct, frozen=True):
    x: int


# --- decoder(cls) ---


def test_decode_decodes_good_bytes() -> None:
    """Decode a matching body into the struct."""
    assert decoder(_Struct)(response(b'{"x": 3}')) == _Struct(x=3)


def test_decode_strict_by_default_rejects_coerced_token() -> None:
    """Reject a string where an ``int`` is declared, since strict is the default."""
    with pytest.raises(DecodeError):
        decoder(_Struct)(response(b'{"x": "3"}'))


def test_decode_lax_opt_in_coerces_string_to_int() -> None:
    """Coerce a quoted number with ``strict=False``, the opt-out for wires that quote numerics."""
    assert decoder(_Struct, strict=False)(response(b'{"x": "3"}')) == _Struct(x=3)


def test_decode_raises_decode_error_on_bad_bytes() -> None:
    """Raise ``DecodeError`` for a body that is not JSON."""
    with pytest.raises(DecodeError):
        decoder(_Struct)(response(b"not json"))


def test_decode_is_the_bytes_level_primitive() -> None:
    """Decode bytes directly, with the same strictness and error as the reader."""
    assert decode(b'{"x": 1}', _Struct) == _Struct(x=1)
    assert decode(b"[]", list[_Struct] | _Struct) == []
    with pytest.raises(DecodeError):
        decode(b'{"x": "1"}', _Struct)


def test_decode_coerces_when_lax() -> None:
    """Coerce a quoted number with ``decode(..., strict=False)``, as the reader does."""
    assert decode(b'{"x": "3"}', _Struct, strict=False) == _Struct(x=3)


class _Noted(msgspec.Struct, frozen=True):
    x: int
    note: str | None = None


def test_a_codec_built_at_run_time_is_strict_and_keeps_none_by_default() -> None:
    """Decode strictly and write ``None`` fields unless told otherwise, for a codec of the caller's own."""
    with pytest.raises(msgspec.ValidationError):
        MsgspecCodec().decoder(_Struct)(b'{"x": "3"}')
    assert MsgspecCodec(strict=False).decoder(_Struct)(b'{"x": "3"}') == _Struct(x=3)
    assert MsgspecCodec().encoder(_Noted)(_Noted(x=1)) == b'{"x":1,"note":null}'
    assert MsgspecCodec(drop_none=True).encoder(_Noted)(_Noted(x=1)) == b'{"x":1}'


# --- decoder(list[cls]): a bare top-level JSON array ---


def test_decode_list_decodes_array() -> None:
    """Decode a bare array of objects."""
    assert decoder(list[_Struct])(response(b'[{"x": 1}, {"x": 2}]')) == [_Struct(x=1), _Struct(x=2)]


def test_decode_list_empty_array_decodes_to_empty_list() -> None:
    """Decode ``[]`` to an empty list."""
    assert decoder(list[_Struct])(response(b"[]")) == []


@pytest.mark.parametrize("body", [b"", b"   ", b"\n", b"  \r\n "])
def test_decode_list_empty_body_raises_by_default(body: bytes) -> None:
    """Fail on an empty 200 by default: a list endpoint sends at least ``[]``, so no body is corruption."""
    with pytest.raises(DecodeError):
        decoder(list[_Struct])(response(body))


@pytest.mark.parametrize("body", [b"", b"   ", b"\n", b"  \r\n "])
def test_decode_list_empty_body_allowed_when_opted_in(body: bytes) -> None:
    """Read an empty body as ``[]`` for an endpoint whose no-data answer is a zero-byte 200."""
    assert decoder(list[_Struct], allow_empty_body=True)(response(body)) == []


@pytest.mark.parametrize("allow", [False, True])
def test_decode_list_truncated_nonempty_body_still_raises(*, allow: bool) -> None:
    """Fail on a truncated non-empty body either way, so transport truncation is never masked as ``[]``."""
    with pytest.raises(DecodeError):
        decoder(list[_Struct], allow_empty_body=allow)(response(b'[{"x": 1}, {"x":'))


def test_decode_list_strict_rejects_coerced_element() -> None:
    """Fail the whole decode for one coerced token in one element."""
    with pytest.raises(DecodeError):
        decoder(list[_Struct], strict=True)(response(b'[{"x": 1}, {"x": "2"}]'))


def test_decode_list_raises_decode_error_on_object_body() -> None:
    """Fail on a top-level object where an array is declared: that is drift, not no-data."""
    with pytest.raises(DecodeError):
        decoder(list[_Struct])(response(b'{"x": 1}'))


# --- decoder(dict[str, cls]): a bare top-level JSON object map ---


def test_decode_map_decodes_object() -> None:
    """Decode a bare object map."""
    result = decoder(dict[str, _Struct])(response(b'{"AAPL": {"x": 1}, "MSFT": {"x": 2}}'))
    assert result == {"AAPL": _Struct(x=1), "MSFT": _Struct(x=2)}


def test_decode_map_empty_object_decodes_to_empty_dict() -> None:
    """Decode ``{}`` to an empty dict."""
    assert decoder(dict[str, _Struct])(response(b"{}")) == {}


def test_decode_map_strict_rejects_coerced_value() -> None:
    """Fail the whole decode for one coerced token in one value."""
    with pytest.raises(DecodeError):
        decoder(dict[str, _Struct], strict=True)(response(b'{"AAPL": {"x": 1}, "MSFT": {"x": "2"}}'))


def test_decode_map_raises_decode_error_on_array_body() -> None:
    """Fail on a top-level array where an object map is declared."""
    with pytest.raises(DecodeError):
        decoder(dict[str, _Struct])(response(b'[{"x": 1}]'))


# --- decoder(dict[str, str]) and decoder(list[list[str]]) ---


def test_decode_str_map_decodes_string_map() -> None:
    """Decode a bare object of strings."""
    result = decoder(dict[str, str], strict=True)(response(b'{"AUD": "Australian Dollar", "EUR": "Euro"}'))
    assert result == {"AUD": "Australian Dollar", "EUR": "Euro"}


@pytest.mark.parametrize("body", [b'{"AUD": 1}', b'["AUD", "EUR"]', b"not json"])
def test_decode_str_map_rejects_drift(body: bytes) -> None:
    """Fail on a non-string value, an array body, or a body that is not JSON."""
    with pytest.raises(DecodeError):
        decoder(dict[str, str], strict=True)(response(body))


def test_decode_str_matrix_decodes_array_of_arrays() -> None:
    """Decode a bare array of string arrays."""
    result = decoder(list[list[str]], strict=True)(response(b'[["cell_value", "time"], ["317.6", "2024-01"]]'))
    assert result == [["cell_value", "time"], ["317.6", "2024-01"]]


@pytest.mark.parametrize("body", [b'[["cell_value"], [317.6]]', b'{"cell_value": "317.6"}', b"not json"])
def test_decode_str_matrix_rejects_drift(body: bytes) -> None:
    """Fail on a number cell, an object body, or a body that is not JSON: every cell is a string."""
    with pytest.raises(DecodeError):
        decoder(list[list[str]], strict=True)(response(body))


# --- to_builtins: struct -> json=-ready builtins ---


class _RenamedOmit(msgspec.Struct, frozen=True, omit_defaults=True):
    plain: str
    bracketed: str = msgspec.field(name="order[0][column]", default="3")
    unset: str | None = None


def test_to_builtins_applies_renames_and_honors_omit_defaults() -> None:
    """Use the wire name, which may be any string, and omit an unset default."""
    assert to_builtins(_RenamedOmit(plain="x")) == {"plain": "x"}
    assert to_builtins(_RenamedOmit(plain="x", bracketed="9", unset="u")) == {
        "plain": "x",
        "order[0][column]": "9",
        "unset": "u",
    }


def test_to_builtins_handles_struct_sequences() -> None:
    """Encode a bare top-level array element by element."""
    assert to_builtins([_Struct(x=1), _Struct(x=2)]) == [{"x": 1}, {"x": 2}]


# --- to_builtins(..., drop_none=True): None-valued fields recursively dropped ---


class _DropInner(msgspec.Struct, frozen=True, kw_only=True):
    a: str | None = None
    b: str | None = None


class _DropOuter(msgspec.Struct, frozen=True, kw_only=True):
    keep_false: bool = False
    maybe: str | None = None
    inner: _DropInner | None = None
    trailing: str | None = None


class _DropContainers(msgspec.Struct, frozen=True):
    listed: list[_DropInner]
    tupled: tuple[_DropInner, ...]


def test_to_builtins_drop_none_drops_none_keeps_false_and_preserves_order() -> None:
    """Drop ``None`` fields at every depth, keep ``False``, and keep declaration order (the wire key order)."""
    body = to_builtins(
        _DropOuter(keep_false=False, maybe=None, inner=_DropInner(a="x"), trailing=None),
        drop_none=True,
    )
    assert body == {"keep_false": False, "inner": {"a": "x"}}
    assert list(body) == ["keep_false", "inner"]


def test_to_builtins_drop_none_keeps_set_fields() -> None:
    """Keep every field that has a value."""
    assert to_builtins(_DropOuter(maybe="present"), drop_none=True) == {"keep_false": False, "maybe": "present"}


def test_to_builtins_drop_none_walks_lists_and_tuples() -> None:
    """Drop ``None`` fields inside list and tuple fields: msgspec keeps a tuple field as a tuple."""
    body = to_builtins(_DropContainers([_DropInner(a="x")], (_DropInner(b="y"),)), drop_none=True)
    assert body == {"listed": [{"a": "x"}], "tupled": ({"b": "y"},)}


# --- What a DecodeError's message repeats ---


class _Side(enum.Enum):
    BUY = "buy"


class _Order(msgspec.Struct, frozen=True):
    side: _Side


def test_a_decode_error_never_repeats_an_unknown_enum_value() -> None:
    """Leave out the one input value msgspec quotes, keeping the diagnosis and its path."""
    with pytest.raises(DecodeError) as raised:
        decode(b'{"side": "sk-live-SECRET"}', _Order)

    assert str(raised.value) == "Invalid enum value - at `$.side`"


def test_a_decode_error_is_bounded_whatever_the_input() -> None:
    """Bound the message even where msgspec would echo an input of any length."""
    with pytest.raises(DecodeError) as raised:
        decode(b'"' + b"x" * (10 * MESSAGE_TEXT_LIMIT) + b'"', _Side)

    assert str(raised.value) == "Invalid enum value"
