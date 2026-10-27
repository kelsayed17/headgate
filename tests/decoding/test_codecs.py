# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""``Codec``: the interface to a serialization library, the readers and writers built on one, and its conformance check.

The core imports no serialization library. These tests drive it with codecs of their own, built on msgspec but
changed in one way each, so that what the core does with a codec, and what ``check_codec`` refuses, is pinned
independently of any one library.
"""

from __future__ import annotations

import importlib
import json
import sys
from typing import TYPE_CHECKING, ClassVar, cast, override

import httpx2
import msgspec
import pytest
from pydantic import BaseModel

from headgate import Codec, EncodedBody, HttpOptions, decode_with, decoder_for, encoder_for, json_writer, nothing
from headgate import msgspec as msgspec_flavor
from headgate import pydantic as pydantic_flavor
from headgate._failures import MESSAGE_TEXT_LIMIT
from headgate.errors import DecodeError, ResponseParseError
from headgate.msgspec import MsgspecCodec
from headgate.testing import FakeTransport, check_codec
from tests.support.clients import StubClient
from tests.support.responses import response

if TYPE_CHECKING:
    from collections.abc import Callable

    from typing_extensions import TypeForm


class _Row(msgspec.Struct, frozen=True):
    """A record with one field, of msgspec's kind."""

    value: int


class _Model(BaseModel):
    """A record with one field, of pydantic's kind."""

    value: int


class _ShapeError(Exception):
    """A library's own error for a body of the wrong shape: no ``ValueError``, so nothing but the codec names it."""


class _Tagged(MsgspecCodec):
    """JSON under a media type of its own, raising its own error type: a codec that is not one of headgate's."""

    media_type: ClassVar[str] = "application/vnd.example+json"
    errors: ClassVar[tuple[type[Exception], ...]] = (_ShapeError,)

    @override
    def decoder[T](self, type_: TypeForm[T], /) -> Callable[[bytes], T]:
        decode = super().decoder(type_)

        def _decode(content: bytes) -> T:
            try:
                return decode(content)
            except msgspec.DecodeError as exc:
                raise _ShapeError(str(exc)) from exc

        return _decode

    @override
    def describe(self, error: Exception, /) -> str:
        return f"shape: {error}"


_TAGGED = _Tagged()


# --- The built-in codecs keep the promises ---


@pytest.mark.parametrize(
    ("codec", "record"),
    [(msgspec_flavor.CODEC, _Row), (pydantic_flavor.CODEC, _Model)],
    ids=["msgspec", "pydantic"],
)
def test_the_built_in_codecs_pass_the_conformance_check(codec: Codec, record: type) -> None:
    """Keep every promise: a media type, a round trip, strict types, malformed bytes refused, quiet messages."""
    check_codec(codec, record=record)


# --- Readers and writers on any codec ---


async def test_a_writer_sends_its_codecs_media_type() -> None:
    """Send the codec's bytes under the codec's media type."""
    fake = FakeTransport().reply("POST", "/rows", 201)
    client = StubClient(base_url="https://api.test", http=HttpOptions(transport=fake))
    await client.post("rows", body=encoder_for(_TAGGED, _Row)(_Row(value=1)), expect=nothing)

    assert fake.calls[0].headers["content-type"] == "application/vnd.example+json"
    assert fake.calls[0].content == b'{"value":1}'


async def test_a_codecs_own_error_is_a_parse_error_with_its_description() -> None:
    """Raise a library's own error as ``ResponseParseError``, described by the codec, never escaping the tree."""
    fake = FakeTransport().reply("GET", "/rows", httpx2.Response(200, json={"value": "one"}))
    client = StubClient(base_url="https://api.test", http=HttpOptions(transport=fake))

    with pytest.raises(ResponseParseError, match=r"shape: Expected `int`, got `str` - at `\$\.value`") as raised:
        await client.get("rows", expect=decoder_for(_TAGGED, _Row))
    assert isinstance(raised.value.__cause__, DecodeError)


def test_an_empty_body_reads_as_the_codecs_empty_array() -> None:
    """Read an empty body as the empty array the codec encodes, when the reader allows it."""
    assert decoder_for(_TAGGED, list[_Row], allow_empty_body=True)(response(b"")) == []


def test_a_json_writer_wraps_a_plain_encode_function() -> None:
    """Send what a plain encode function writes as JSON, for a writer that is not built on a codec."""
    assert json_writer(lambda value: json.dumps(value).encode())({"a": 1}) == EncodedBody(
        b'{"a": 1}', "application/json"
    )


def test_a_description_is_bounded() -> None:
    """Cut a long description to the bound every decode message keeps."""

    class _Wordy(_Tagged):
        @override
        def describe(self, error: Exception, /) -> str:
            return "x" * 5000

    with pytest.raises(DecodeError) as raised:
        decode_with(_Wordy(), b"{", _Row)
    assert len(str(raised.value)) <= MESSAGE_TEXT_LIMIT


# --- The conformance check refuses a codec that breaks a promise ---


class _BadMediaType(MsgspecCodec):
    media_type: ClassVar[str] = "json"


class _ForgetsValues(MsgspecCodec):
    @override
    def decoder[T](self, type_: TypeForm[T], /) -> Callable[[bytes], T]:
        decode = super().decoder(type_)

        def _decode(content: bytes) -> T:
            decoded = decode(content)
            return type(decoded)() if isinstance(decoded, dict) else decoded

        return _decode


class _IgnoresTypes(MsgspecCodec):
    @override
    def decoder[T](self, type_: TypeForm[T], /) -> Callable[[bytes], T]:
        return cast("Callable[[bytes], T]", super().decoder(object))


class _QuotesTheInput(MsgspecCodec):
    @override
    def decoder[T](self, type_: TypeForm[T], /) -> Callable[[bytes], T]:
        decode = super().decoder(type_)

        def _decode(content: bytes) -> T:
            try:
                return decode(content)
            except msgspec.DecodeError as exc:
                msg = f"bad body {content!r}"
                raise msgspec.ValidationError(msg) from exc

        return _decode


class _UnlistedError(MsgspecCodec):
    errors: ClassVar[tuple[type[Exception], ...]] = (_ShapeError,)


class _RejectsUnknownFields(MsgspecCodec):
    @override
    def decoder[T](self, type_: TypeForm[T], /) -> Callable[[bytes], T]:
        decode = super().decoder(type_)

        def _decode(content: bytes) -> T:
            if b"added_by_the_provider" in content:
                msg = "unknown field"
                raise msgspec.ValidationError(msg)
            return decode(content)

        return _decode


class _MisreadsRecords(MsgspecCodec):
    @override
    def decoder[T](self, type_: TypeForm[T], /) -> Callable[[bytes], T]:
        decode = super().decoder(type_)
        if type_ is _Row:
            return lambda _content: decode(b'{"value": 2}')
        return decode


@pytest.mark.parametrize(
    ("codec", "message"),
    [
        (_BadMediaType(), r"media_type 'json' is not a media type"),
        (_ForgetsValues(), r"wrote \{'a': 1\} and read back \{\}"),
        (_IgnoresTypes(), r"accepted a string where an int is declared"),
        (_QuotesTheInput(), r"describe\(\) repeats the input"),
        (_UnlistedError(), r"raised ValidationError for a string where an int is declared, which is not one of"),
        (_RejectsUnknownFields(), r"refused a body with a field .* does not declare"),
        (_MisreadsRecords(), r"read _Row\(value=2\) from a body with a field"),
    ],
    ids=["media-type", "round-trip", "strict", "quiet", "errors", "unknown-field", "record"],
)
def test_the_conformance_check_names_the_promise_a_codec_breaks(codec: Codec, message: str) -> None:
    """Fail with a message naming the codec and the promise it broke."""
    with pytest.raises(AssertionError, match=message):
        check_codec(codec, record=_Row)


def test_the_unknown_field_promise_needs_a_record() -> None:
    """Check every other promise when no record type is given."""
    check_codec(_RejectsUnknownFields())


# --- msgspec is an extra ---


def test_importing_without_msgspec_names_the_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail the import with the install hint when msgspec is missing, chained to the original ``ImportError``."""
    monkeypatch.setitem(sys.modules, "msgspec", None)
    monkeypatch.delitem(sys.modules, "headgate.msgspec")
    with pytest.raises(ImportError, match=r"pip install headgate\[msgspec\]") as caught:
        importlib.import_module("headgate.msgspec")
    assert isinstance(caught.value.__cause__, ImportError)
