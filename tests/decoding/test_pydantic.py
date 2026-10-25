# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""Unit tests for :mod:`headgate.pydantic`: the strict pydantic codec flavor, held to the msgspec flavor's contract.

Strictness parity is the acceptance test: a JSON string where an ``int`` is declared fails loud, an unknown field is
ignored, and every failure is the shared ``DecodeError``.
"""

from __future__ import annotations

import msgspec
import pytest
from pydantic import BaseModel, ConfigDict, Field

from headgate import msgspec as msgspec_flavor
from headgate._responses import Refused
from headgate.errors import DecodeError
from headgate.pydantic import decode, decoder, to_builtins
from tests.support.responses import response


class _Model(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore", frozen=True)

    x: int


class _Struct(msgspec.Struct, frozen=True):
    x: int


class _Env(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore", frozen=True)

    status: str
    value: int | None = None


def _quota_check(env: _Env) -> Refused | None:
    return Refused("over quota", code="Q") if env.status == "QUOTA" else None


def test_decoder_reads_a_body_through_the_response() -> None:
    assert decoder(_Model)(response(b'{"x": 3}')) == _Model(x=3)


def test_string_for_int_raises_decode_error() -> None:
    with pytest.raises(DecodeError):
        decoder(_Model)(response(b'{"x": "3"}'))


def test_unknown_fields_ignored() -> None:
    assert decoder(_Model)(response(b'{"x": 1, "extra": [1, 2]}')) == _Model(x=1)


def test_lax_opt_in_coerces_a_string_to_an_int() -> None:
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
def test_strictness_matches_the_msgspec_flavor(body: bytes, accepted: bool) -> None:
    """Both flavors accept and reject the same bodies, so switching flavor never changes what counts as drift."""

    def outcome(read: object) -> bool:
        try:
            read(response(body))  # ty: ignore[call-non-callable]  # the two readers are called identically
        except DecodeError:
            return False
        return True

    assert outcome(decoder(_Model)) is accepted
    assert outcome(msgspec_flavor.decoder(_Struct)) is accepted


def test_one_malformed_element_fails_the_whole_list() -> None:
    with pytest.raises(DecodeError):
        decoder(list[_Model])(response(b'[{"x": 1}, {"x": "x"}]'))


def test_a_union_of_shapes_decodes_either() -> None:
    read = decoder(list[_Model] | _Model)
    assert read(response(b"[]")) == []
    assert read(response(b'{"x": 1}')) == _Model(x=1)


def test_null_decodes_for_an_optional_type() -> None:
    assert decoder(_Model | None)(response(b"null")) is None


def test_allow_empty_body_reads_zero_bytes_as_the_empty_array() -> None:
    assert decoder(list[_Model], allow_empty_body=True)(response(b"")) == []
    with pytest.raises(DecodeError):
        decoder(list[_Model])(response(b""))


def test_a_check_that_refuses_raises_the_refused_it_returned() -> None:
    read = decoder(_Env, check=_quota_check)
    with pytest.raises(Refused) as caught:
        read(response(b'{"status": "QUOTA"}'))
    assert caught.value.code == "Q"
    assert read(response(b'{"status": "OK"}')) == _Env(status="OK")


def test_decode_is_the_bytes_level_primitive() -> None:
    assert decode(b'{"x": 2}', _Model) == _Model(x=2)
    with pytest.raises(DecodeError):
        decode(b"nope", _Model)


class _Aliased(BaseModel):
    order_id: str = Field(alias="orderId")
    note: str | None = None


def test_to_builtins_applies_aliases() -> None:
    assert to_builtins(_Aliased(orderId="a1", note="n")) == {"orderId": "a1", "note": "n"}


def test_to_builtins_keeps_none_unless_dropped() -> None:
    model = _Aliased(orderId="a1")
    assert to_builtins(model) == {"orderId": "a1", "note": None}
    assert to_builtins(model, drop_none=True) == {"orderId": "a1"}


def test_to_builtins_handles_sequences_and_nested_models() -> None:
    assert to_builtins([_Model(x=1), _Model(x=2)]) == [{"x": 1}, {"x": 2}]
    assert to_builtins({"inner": _Aliased(orderId="a", note=None)}, drop_none=True) == {"inner": {"orderId": "a"}}


def test_drop_none_keeps_false_and_zero() -> None:
    class Flags(BaseModel):
        on: bool | None = None
        n: int | None = None

    assert to_builtins(Flags(on=False, n=0), drop_none=True) == {"on": False, "n": 0}
