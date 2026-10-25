"""Unit tests for :mod:`headgate.msgspec`: the strict msgspec codec flavor.

Covers ``to_builtins`` directly, plus ``decode`` and the neutral :class:`DecodeError` through the reader ``decoder``
builds:

- ``decode``: strict msgspec JSON decode, raising ``DecodeError`` on bad bytes (``strict=True`` rejects a coerced token
  that lax mode accepts).
- ``to_builtins``: the encode side, struct -> ``json=``-ready builtins (httpx2 owns the JSON serialization);
  ``drop_none=True`` recursively removes None-valued fields.
"""

from __future__ import annotations

import msgspec
import pytest

from headgate.errors import DecodeError
from headgate.msgspec import decoder, to_builtins
from tests.support.responses import response


class _Struct(msgspec.Struct, frozen=True):
    x: int


# ---------------------------------------------------------------------------
# decode(cls, strict=...)
# ---------------------------------------------------------------------------


def test_decode_decodes_good_bytes() -> None:
    dec = decoder(_Struct)
    result = dec(response(b'{"x": 3}'))
    assert result == _Struct(x=3)


def test_decode_strict_by_default_rejects_coerced_token() -> None:
    # strict=True is the DEFAULT (the ring's fidelity doctrine): a coerced
    # token (string where an int is declared) is wire drift and fails loud.
    dec = decoder(_Struct)
    with pytest.raises(DecodeError):
        dec(response(b'{"x": "3"}'))


def test_decode_lax_optin_coerces_string_to_int() -> None:
    # strict=False is the explicit opt-out for wires that genuinely quote
    # numerics.
    dec = decoder(_Struct, strict=False)
    result = dec(response(b'{"x": "3"}'))
    assert result == _Struct(x=3)


def test_decode_raises_decode_error_on_bad_bytes() -> None:
    dec = decoder(_Struct)
    with pytest.raises(DecodeError):
        dec(response(b"not json"))


# ---------------------------------------------------------------------------
# decode(list[cls], strict=...) — bare top-level JSON array
# ---------------------------------------------------------------------------


def test_decode_list_decodes_array() -> None:
    dec = decoder(list[_Struct])
    result = dec(response(b'[{"x": 1}, {"x": 2}]'))
    assert result == [_Struct(x=1), _Struct(x=2)]


def test_decode_list_empty_array_decodes_to_empty_list() -> None:
    dec = decoder(list[_Struct])
    assert dec(response(b"[]")) == []


@pytest.mark.parametrize("body", [b"", b"   ", b"\n", b"  \r\n "])
def test_decode_list_empty_body_raises_by_default(body: bytes) -> None:
    # DEFAULT is strict: for almost every provider a 200 with no body is corruption
    # (coin_gecko's /coins/markets always sends at least b"[]"), so it must fail loud.
    dec = decoder(list[_Struct])
    with pytest.raises(DecodeError):
        dec(response(body))


@pytest.mark.parametrize("body", [b"", b"   ", b"\n", b"  \r\n "])
def test_decode_list_empty_body_allowed_when_opted_in(body: bytes) -> None:
    # Opt-in only: FINRA's otcMarket/name/* query endpoints answer no-data with a
    # ZERO-BYTE 200 (not b"[]"), which msgspec rejects as "Input data was truncated".
    dec = decoder(list[_Struct], allow_empty_body=True)
    assert dec(response(body)) == []


@pytest.mark.parametrize("allow", [False, True])
def test_decode_list_truncated_nonempty_body_still_raises(allow: bool) -> None:
    # A partial/truncated (NON-empty) body is corruption, NOT no-data — it must fail
    # loud under BOTH settings so genuine transport truncation is never masked as [].
    dec = decoder(list[_Struct], allow_empty_body=allow)
    with pytest.raises(DecodeError):
        dec(response(b'[{"x": 1}, {"x":'))


def test_decode_list_strict_rejects_coerced_element() -> None:
    # One coerced token in one element fails the whole atomic decode under strict.
    dec = decoder(list[_Struct], strict=True)
    with pytest.raises(DecodeError):
        dec(response(b'[{"x": 1}, {"x": "2"}]'))


def test_decode_list_raises_decode_error_on_object_body() -> None:
    # A top-level object where an array is expected is drift, not no-data.
    dec = decoder(list[_Struct])
    with pytest.raises(DecodeError):
        dec(response(b'{"x": 1}'))


# ---------------------------------------------------------------------------
# decode(dict[str, cls], strict=...) — bare top-level JSON object map
# ---------------------------------------------------------------------------


def test_decode_map_decodes_object() -> None:
    dec = decoder(dict[str, _Struct])
    result = dec(response(b'{"AAPL": {"x": 1}, "MSFT": {"x": 2}}'))
    assert result == {"AAPL": _Struct(x=1), "MSFT": _Struct(x=2)}


def test_decode_map_empty_object_decodes_to_empty_dict() -> None:
    dec = decoder(dict[str, _Struct])
    assert dec(response(b"{}")) == {}


def test_decode_map_strict_rejects_coerced_value() -> None:
    # One coerced token in one value fails the whole atomic decode under strict.
    dec = decoder(dict[str, _Struct], strict=True)
    with pytest.raises(DecodeError):
        dec(response(b'{"AAPL": {"x": 1}, "MSFT": {"x": "2"}}'))


def test_decode_map_raises_decode_error_on_array_body() -> None:
    # A top-level array where an object map is expected is drift, not no-data.
    dec = decoder(dict[str, _Struct])
    with pytest.raises(DecodeError):
        dec(response(b'[{"x": 1}]'))


# ---------------------------------------------------------------------------
# decode(dict[str, str]) — bare top-level JSON object with STRING values
# ---------------------------------------------------------------------------


def test_decode_str_map_decodes_string_map() -> None:
    result = decoder(dict[str, str], strict=True)(response(b'{"AUD": "Australian Dollar", "EUR": "Euro"}'))
    assert result == {"AUD": "Australian Dollar", "EUR": "Euro"}


def test_decode_str_map_empty_object_decodes_to_empty_dict() -> None:
    assert decoder(dict[str, str], strict=True)(response(b"{}")) == {}


def test_decode_str_map_strict_rejects_non_string_value() -> None:
    # A non-string value fails the strict dict[str, str] decode (drift, not no-data).
    with pytest.raises(DecodeError):
        decoder(dict[str, str], strict=True)(response(b'{"AUD": 1}'))


def test_decode_str_map_raises_decode_error_on_array_body() -> None:
    # A top-level array where an object map is expected is drift.
    with pytest.raises(DecodeError):
        decoder(dict[str, str], strict=True)(response(b'["AUD", "EUR"]'))


def test_decode_str_map_raises_decode_error_on_bad_bytes() -> None:
    with pytest.raises(DecodeError):
        decoder(dict[str, str], strict=True)(response(b"not json"))


# ---------------------------------------------------------------------------
# decode(list[list[str]]) — bare top-level JSON array-of-arrays of STRINGS
# ---------------------------------------------------------------------------


def test_decode_str_matrix_decodes_array_of_arrays() -> None:
    result = decoder(list[list[str]], strict=True)(response(b'[["cell_value", "time"], ["317.6", "2024-01"]]'))
    assert result == [["cell_value", "time"], ["317.6", "2024-01"]]


def test_decode_str_matrix_empty_array_decodes_to_empty_list() -> None:
    assert decoder(list[list[str]], strict=True)(response(b"[]")) == []


def test_decode_str_matrix_strict_rejects_non_string_cell() -> None:
    # A bare JSON number cell fails the strict list[list[str]] decode (drift) —
    # the "every cell is a string" fidelity contract.
    with pytest.raises(DecodeError):
        decoder(list[list[str]], strict=True)(response(b'[["cell_value"], [317.6]]'))


def test_decode_str_matrix_raises_decode_error_on_object_body() -> None:
    # A top-level object where an array-of-arrays is expected is drift.
    with pytest.raises(DecodeError):
        decoder(list[list[str]], strict=True)(response(b'{"cell_value": "317.6"}'))


def test_decode_str_matrix_raises_decode_error_on_bad_bytes() -> None:
    with pytest.raises(DecodeError):
        decoder(list[list[str]], strict=True)(response(b"not json"))


# ---------------------------------------------------------------------------
# encode — msgspec struct -> json=-ready builtins (thin delegate; the
# clients' single body-prep import surface)
# ---------------------------------------------------------------------------


class _RenamedOmit(msgspec.Struct, frozen=True, omit_defaults=True):
    plain: str
    bracketed: str = msgspec.field(name="order[0][column]", default="3")
    unset: str | None = None


def test_encode_applies_renames_and_honors_omit_defaults() -> None:
    # Both behaviors are load-bearing at call sites (DataTables bracketed keys;
    # the BLS keyless body omitting registrationkey) — pinned here, measured.
    assert to_builtins(_RenamedOmit(plain="x")) == {"plain": "x"}
    assert to_builtins(_RenamedOmit(plain="x", bracketed="9", unset="u")) == {
        "plain": "x",
        "order[0][column]": "9",
        "unset": "u",
    }


def test_encode_handles_struct_sequences() -> None:
    # A bare array body (the OpenFIGI mapping batch) rides the same delegate.
    assert to_builtins([_Struct(x=1), _Struct(x=2)]) == [{"x": 1}, {"x": 2}]


# ---------------------------------------------------------------------------
# encode(..., drop_none=True) — None-valued fields recursively dropped (for
# outbound bodies whose unset optionals must be omitted)
# ---------------------------------------------------------------------------


class _DropInner(msgspec.Struct, frozen=True, kw_only=True):
    a: str | None = None
    b: str | None = None


class _DropOuter(msgspec.Struct, frozen=True, kw_only=True):
    keep_false: bool = False
    maybe: str | None = None
    inner: _DropInner | None = None
    trailing: str | None = None


def test_encode_drop_none_drops_none_keeps_false_and_preserves_order() -> None:
    # None fields (maybe, trailing, inner.b) are dropped; the bool False is KEPT
    # (the exclude_none vs omit_defaults distinction that pins post_only=false);
    # field declaration order is preserved via to_builtins (dict order is wire
    # key order once httpx2 serializes the json= builtins).
    body = to_builtins(
        _DropOuter(keep_false=False, maybe=None, inner=_DropInner(a="x"), trailing=None),
        drop_none=True,
    )
    assert body == {"keep_false": False, "inner": {"a": "x"}}
    assert list(body) == ["keep_false", "inner"]


def test_encode_drop_none_returns_builtins() -> None:
    body = to_builtins(_DropOuter(maybe="present"), drop_none=True)
    assert body == {"keep_false": False, "maybe": "present"}
