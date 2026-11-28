# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""A reader that cannot work fails where it is built, and a reader's own misuse is never blamed on the body.

``Reader(str)`` would return the response's ``repr``; a msgspec reader of a pydantic model or a plain class would
blame every good body on the server; a ``refuse=`` or ``response_check=`` that returns what is no refusal is the
caller's bug, not a body of the wrong shape.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final, cast, override

import httpx2
import msgspec
import pydantic
import pytest

import headgate.msgspec
import headgate.pydantic
from headgate import Client, HttpOptions, Reader, UpstreamStatusError, body_reader
from headgate._failures import ReaderMisuseError, mint_error
from headgate.errors import RefusedError, ResponseParseError
from headgate.msgspec import MsgspecCodec
from headgate.testing import FakeTransport, check_codec

if TYPE_CHECKING:
    from collections.abc import Callable

    from typing_extensions import TypeForm

_BASE: Final = "https://api.example.test/"


class _Plain:
    """A class neither msgspec nor any codec knows how to build."""


class _Model(pydantic.BaseModel):
    """A pydantic model, which only the pydantic flavor decodes."""

    name: str


class _Row(msgspec.Struct, frozen=True):
    """A record of the msgspec codec's own kind."""

    value: int


@pytest.mark.parametrize("cls", [str, int, _Plain], ids=["str", "int", "plain"])
def test_a_reader_refuses_a_class(cls: type) -> None:
    """Refuse a class, whose call on the response is no reading of it, pointing to ``as_text`` and a lambda."""
    with pytest.raises(TypeError, match=rf"^Reader takes a function of the response \(got the class {cls.__name__}\)"):
        Reader(cast("Callable[[httpx2.Response], object]", cls))
    with pytest.raises(TypeError, match=r"as_text.*Reader\(lambda r: "):
        Reader(cast("Callable[[httpx2.Response], object]", cls))


def test_a_reader_takes_a_function_and_a_callable_object() -> None:
    """Accept a function and an instance with ``__call__``, which are functions of the response."""

    class Status:
        def __call__(self, response: httpx2.Response, /) -> int:
            return response.status_code

    assert repr(Reader(Status())) == "Reader(Status)"

    @Reader
    def status(response: httpx2.Response, /) -> int:
        return response.status_code

    assert repr(status) == "Reader(status)"


def test_the_msgspec_flavor_refuses_a_pydantic_model() -> None:
    """Refuse a pydantic model where the reader is built, naming the flavor that decodes it."""
    with pytest.raises(
        TypeError,
        match=r"^headgate\.msgspec cannot decode _Model, a pydantic model: use headgate\.pydantic\.reader\(_Model\)",
    ):
        headgate.msgspec.reader(_Model)


@pytest.mark.parametrize(
    "type_", [_Plain, list[_Plain], dict[str, _Plain] | None], ids=["class", "list", "optional-dict"]
)
def test_the_msgspec_flavor_refuses_a_class_it_does_not_know(type_: TypeForm[object]) -> None:
    """Refuse a plain class, anywhere in the type, where the reader is built, naming it and what msgspec decodes."""
    with pytest.raises(TypeError, match=r"^headgate\.msgspec cannot decode _Plain, a class msgspec does not know: "):
        headgate.msgspec.reader(type_)


def test_the_msgspec_flavor_decodes_object_as_any_value() -> None:
    """Accept ``object``, which msgspec decodes as any JSON value."""
    assert headgate.msgspec.decode(b"[1]", object) == [1]


@pytest.mark.parametrize(
    ("type_", "name"), [(_Plain, "_Plain"), (list[_Plain], "_Plain"), (_Row, "_Row")], ids=["class", "list", "struct"]
)
def test_the_pydantic_flavor_refuses_a_class_it_has_no_schema_for(type_: TypeForm[object], name: str) -> None:
    """Refuse a class pydantic has no schema for with ``TypeError``, as the msgspec flavor does, naming the class."""
    with pytest.raises(TypeError, match=rf"^headgate\.pydantic cannot decode {name}, a class pydantic has no schema "):
        headgate.pydantic.reader(type_)


class _TrustsEveryClass(MsgspecCodec):
    """A codec whose decoder builds for any class, and fails only at the first body."""

    @override
    def decoder[T](self, type_: TypeForm[T], /) -> Callable[[bytes], T]:
        return cast("Callable[[bytes], T]", msgspec.json.Decoder(type_).decode)


def test_the_conformance_check_asks_a_codec_to_refuse_an_unknown_class() -> None:
    """Fail a codec that builds a decoder for a class it cannot decode, which would blame every body on the server."""
    with pytest.raises(AssertionError, match=r"^_TrustsEveryClass built a decoder for a class it cannot decode"):
        check_codec(_TrustsEveryClass(), record=_Row)


def _client(*replies: httpx2.Response) -> Client:
    return Client(base_url=_BASE, http=HttpOptions(transport=FakeTransport().reply("GET", "/x", *replies)))


async def test_a_refuse_that_returns_no_refusal_is_the_callers_bug() -> None:
    """Raise ``TypeError`` naming ``refuse=`` for a check that returns a string, not ``ResponseParseError``."""
    read = body_reader(bytes, refuse=cast("Callable[[bytes], RefusedError | None]", lambda body: body.decode()))
    async with _client(httpx2.Response(200, content=b"x")) as client:
        with pytest.raises(
            TypeError, match=r"^refuse= returns a RefusedError, or None to accept the value \(got str\)"
        ):
            await client.get("x", expect=read)


def test_a_refusal_is_raised_as_an_upstream_status_error() -> None:
    """Refuse a ``kind`` that is no ``UpstreamStatusError``, which the client could not raise the refusal as."""
    with pytest.raises(TypeError, match=r"^kind= takes an UpstreamStatusError subclass, such as AuthError .*\(got"):
        RefusedError("no", kind=cast("Any", ValueError))
    with pytest.raises(TypeError, match=r"^kind= takes an UpstreamStatusError subclass"):
        RefusedError("no", kind=cast("Any", "auth"))
    assert RefusedError("no", kind=UpstreamStatusError).kind is UpstreamStatusError


async def test_a_response_check_that_returns_no_refusal_is_the_callers_bug() -> None:
    """Raise ``TypeError`` naming ``response_check=`` for a check that returns a string."""
    fake = FakeTransport().reply("GET", "/x", 200)
    check = cast("Callable[[httpx2.Response], RefusedError | None]", lambda response: response.reason_phrase)
    http = HttpOptions(transport=fake, response_check=check, follow_redirects=False)
    async with Client(base_url=_BASE, http=http) as client:
        with pytest.raises(TypeError, match=r"^response_check= returns a RefusedError, or None to accept the response"):
            await client.get("x", expect=headgate.as_bytes)


def test_a_token_mint_raises_a_readers_misuse_as_it_is() -> None:
    """Leave a reader's misuse in a token mint unmapped, where any other ``TypeError`` reads as an unreadable token."""
    misuse = ReaderMisuseError("refuse= returns a RefusedError, or None to accept the value (got str)")

    assert mint_error(misuse, source="tokens") is misuse
    assert isinstance(mint_error(TypeError("shape"), source="tokens"), ResponseParseError)
