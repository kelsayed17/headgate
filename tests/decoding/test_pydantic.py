# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""Unit tests for :mod:`headgate.pydantic`, the strict pydantic codec flavor, held to the msgspec flavor's contract.

Strictness parity is the acceptance test: a JSON string where an ``int`` is declared fails, an unknown field is
ignored, and every failure is the shared ``DecodeError``.
"""

from __future__ import annotations

import importlib
import sys
from typing import TYPE_CHECKING

import msgspec
import pytest
from pydantic import BaseModel, ConfigDict, Field, RootModel

from headgate import msgspec as msgspec_flavor
from headgate._failures import MESSAGE_TEXT_LIMIT
from headgate._responses import RefusedError
from headgate.errors import DecodeError
from headgate.pydantic import decode, decoder, to_builtins
from tests.support.responses import response

if TYPE_CHECKING:
    from headgate._responses import Reader


class _Model(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore", frozen=True)

    x: int


class _Struct(msgspec.Struct, frozen=True):
    x: int


class _Env(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore", frozen=True)

    status: str
    value: int | None = None


def _quota_check(env: _Env) -> RefusedError | None:
    """Refuse a ``QUOTA`` envelope, and pass every other one."""
    return RefusedError("over quota", code="Q") if env.status == "QUOTA" else None


def test_decoder_reads_a_body_through_the_response() -> None:
    """Validate a matching body into the model."""
    assert decoder(_Model)(response(b'{"x": 3}')) == _Model(x=3)


def test_string_for_int_raises_decode_error() -> None:
    """Reject a string where an ``int`` is declared, since strict is the default."""
    with pytest.raises(DecodeError):
        decoder(_Model)(response(b'{"x": "3"}'))


def test_unknown_fields_ignored() -> None:
    """Ignore a field the model does not declare."""
    assert decoder(_Model)(response(b'{"x": 1, "extra": [1, 2]}')) == _Model(x=1)


def test_lax_opt_in_coerces_a_string_to_an_int() -> None:
    """Coerce a quoted number with ``strict=False``."""
    assert decoder(_Model, strict=False)(response(b'{"x": "3"}')) == _Model(x=3)


@pytest.mark.parametrize(
    ("body", "accepted"),
    [
        (b'{"x": "1"}', False),
        (b'{"x": 1, "extra": 2}', True),
        (b'{"x": 1.0}', False),
        (b'{"x": true}', False),
        (b'{"x": 1', False),
        (b"", False),
    ],
    ids=["string-for-int", "unknown-field", "float-for-int", "bool-for-int", "truncated", "empty"],
)
def test_strictness_matches_the_msgspec_flavor(body: bytes, *, accepted: bool) -> None:
    """Accept and reject the same bodies in both flavors, so switching flavor never changes what counts as drift."""

    def outcome(read: Reader[object]) -> bool:
        try:
            read(response(body))
        except DecodeError:
            return False
        return True

    assert outcome(decoder(_Model)) is accepted
    assert outcome(msgspec_flavor.decoder(_Struct)) is accepted


def test_one_malformed_element_fails_the_whole_list() -> None:
    """Fail the whole body for one malformed element."""
    with pytest.raises(DecodeError):
        decoder(list[_Model])(response(b'[{"x": 1}, {"x": "x"}]'))


def test_a_union_of_shapes_decodes_either() -> None:
    """Validate either arm of a union of shapes."""
    read = decoder(list[_Model] | _Model)
    assert read(response(b"[]")) == []
    assert read(response(b'{"x": 1}')) == _Model(x=1)


def test_null_decodes_for_an_optional_type() -> None:
    """Validate ``null`` as ``None`` for an optional type."""
    assert decoder(_Model | None)(response(b"null")) is None


def test_allow_empty_body_reads_zero_bytes_as_the_empty_array() -> None:
    """Read an empty body as ``[]`` only when opted in."""
    assert decoder(list[_Model], allow_empty_body=True)(response(b"")) == []
    with pytest.raises(DecodeError):
        decoder(list[_Model])(response(b""))


def test_a_check_that_refuses_raises_the_refused_it_returned() -> None:
    """Raise the ``RefusedError`` a check returns, and return the value when it returns ``None``."""
    read = decoder(_Env, check=_quota_check)
    with pytest.raises(RefusedError) as caught:
        read(response(b'{"status": "QUOTA"}'))
    assert caught.value.code == "Q"
    assert read(response(b'{"status": "OK"}')) == _Env(status="OK")


def test_decode_is_the_bytes_level_primitive() -> None:
    """Validate bytes directly, with the same error as the reader."""
    assert decode(b'{"x": 2}', _Model) == _Model(x=2)
    with pytest.raises(DecodeError):
        decode(b"nope", _Model)


class _Aliased(BaseModel):
    order_id: str = Field(alias="orderId")
    note: str | None = None


def test_to_builtins_applies_aliases() -> None:
    """Use each field's alias as its wire name."""
    assert to_builtins(_Aliased(orderId="a1", note="n")) == {"orderId": "a1", "note": "n"}


def test_to_builtins_keeps_none_unless_dropped() -> None:
    """Keep a ``None`` field unless ``drop_none=True``."""
    model = _Aliased(orderId="a1")
    assert to_builtins(model) == {"orderId": "a1", "note": None}
    assert to_builtins(model, drop_none=True) == {"orderId": "a1"}


def test_to_builtins_handles_sequences_and_nested_models() -> None:
    """Convert a list of models and a mapping holding one."""
    assert to_builtins([_Model(x=1), _Model(x=2)]) == [{"x": 1}, {"x": 2}]
    assert to_builtins({"inner": _Aliased(orderId="a", note=None)}, drop_none=True) == {"inner": {"orderId": "a"}}


def test_to_builtins_converts_a_root_model_to_its_root() -> None:
    """Convert a ``RootModel`` to whatever its root converts to."""
    assert to_builtins(RootModel[list[_Model]]([_Model(x=1)])) == [{"x": 1}]


def test_drop_none_keeps_false_and_zero() -> None:
    """Keep ``False`` and ``0`` when dropping ``None`` fields."""

    class Flags(BaseModel):
        on: bool | None = None
        n: int | None = None

    assert to_builtins(Flags(on=False, n=0), drop_none=True) == {"on": False, "n": 0}


def test_both_flavors_build_the_same_body() -> None:
    """Build the same builtins from equivalent wire types in both flavors."""

    class Row(BaseModel):
        x: int
        note: str | None = None

    class RowStruct(msgspec.Struct, frozen=True):
        x: int
        note: str | None = None

    rows, structs = [Row(x=1), Row(x=2, note="n")], [RowStruct(x=1), RowStruct(x=2, note="n")]
    assert to_builtins(rows, drop_none=True) == msgspec_flavor.to_builtins(structs, drop_none=True)
    assert to_builtins(rows) == msgspec_flavor.to_builtins(structs)


def test_importing_without_pydantic_names_the_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail the import with the install hint when pydantic is missing, chained to the original ``ImportError``.

    ``None`` in ``sys.modules`` makes ``import pydantic`` raise ``ImportError``, and removing ``headgate.pydantic``
    makes the import execute the module afresh. ``monkeypatch`` restores all three entries afterwards.
    """
    monkeypatch.setitem(sys.modules, "pydantic", None)
    monkeypatch.setitem(sys.modules, "pydantic_core", None)
    monkeypatch.delitem(sys.modules, "headgate.pydantic")
    with pytest.raises(ImportError, match=r"pip install headgate\[pydantic\]") as caught:
        importlib.import_module("headgate.pydantic")
    assert isinstance(caught.value.__cause__, ImportError)


class _Credential(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore", frozen=True)

    token: int


#: Enough malformed rows that ``str(ValidationError)`` would run to most of a megabyte.
_BAD_ROWS = 5000


def test_a_decode_error_names_a_few_failures_and_counts_the_rest_within_a_bound() -> None:
    """Summarize thousands of failures as the first few by location, and how many there were, in a bounded message."""
    body = b"[" + b",".join([b'{"x": "bad"}'] * _BAD_ROWS) + b"]"

    with pytest.raises(DecodeError) as raised:
        decode(body, list[_Model])

    message = str(raised.value)
    assert len(message) <= MESSAGE_TEXT_LIMIT
    assert message.startswith(f"{_BAD_ROWS} validation errors for list[_Model]: 0.x: Input should be a valid integer")
    assert message.endswith(f"(+{_BAD_ROWS - 5} more)")


def test_a_decode_error_never_repeats_an_input_value() -> None:
    """Leave out the value that failed, which for a credential field is the credential itself."""
    with pytest.raises(DecodeError) as raised:
        decode(b'{"token": "sk-live-SECRET"}', _Credential)

    assert "SECRET" not in str(raised.value)
    assert str(raised.value) == "1 validation error for _Credential: token: Input should be a valid integer"


def test_a_decode_error_for_malformed_json_names_the_body() -> None:
    """Name the whole body as the location of a failure that has none, such as invalid JSON."""
    with pytest.raises(DecodeError, match=r"^1 validation error for _Model: <body>: Invalid JSON"):
        decode(b'{"x":', _Model)


def test_decode_is_strict_by_default_whatever_the_type() -> None:
    """Refuse a JSON string where an ``int`` is declared, for a bare type with no strict config of its own."""
    with pytest.raises(DecodeError, match="1 validation error for int"):
        decode(b'"1"', int)


class _Pair(BaseModel):
    a: int
    b: int


def test_each_failure_is_named_and_separated() -> None:
    """Name each failing field with its message, separated by ``"; "``."""
    with pytest.raises(DecodeError) as raised:
        decode(b"{}", _Pair)

    assert str(raised.value) == "2 validation errors for _Pair: a: Field required; b: Field required"
