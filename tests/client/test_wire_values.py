# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""Only values with a wire meaning are sent in a query or a form: one allowlist encodes them all.

``params=``, ``Form`` fields and ``Multipart`` fields take text, numbers, bools and ``None`` as they are; an ``Enum``
as its value; a ``UUID`` or ``Decimal`` as its text; a ``date``, ``datetime`` or ``time`` in ISO 8601. Anything else
(a dataclass, a struct, a model, ``object()``) would go out as its Python ``repr``, so it raises ``TypeError`` naming
the key instead.
"""

from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from decimal import Decimal
from urllib.parse import parse_qs

import httpx2
import msgspec
import pytest

from headgate import Client, Form, HttpOptions, Multipart, nothing
from headgate.msgspec import reader
from headgate.pagination import LinkHeader
from headgate.testing import FakeTransport


class _Status(enum.Enum):
    OPEN = "open"


class _Level(enum.IntEnum):
    HIGH = 3


class _Color(enum.StrEnum):
    RED = "red"


class _Ratio(enum.Enum):
    HALF = 0.5


@dataclass(frozen=True)
class _Order:
    qty: int


class _Struct(msgspec.Struct):
    title: str


_ID = uuid.UUID("12345678-1234-5678-1234-567812345678")

#: Each value with a wire meaning, and the text it is sent as.
_WIRE = [
    (_Status.OPEN, "open"),
    (_Level.HIGH, "3"),
    (_Color.RED, "red"),
    (_ID, "12345678-1234-5678-1234-567812345678"),
    (Decimal("1.50"), "1.50"),
    (date(2026, 1, 2), "2026-01-02"),
    (datetime(2026, 1, 2, 3, 4, tzinfo=UTC), "2026-01-02T03:04:00+00:00"),
    (time(3, 4), "03:04:00"),
    ("text", "text"),
    (7, "7"),
    (True, "true"),
]
_WIRE_IDS = ["enum", "int-enum", "str-enum", "uuid", "decimal", "date", "datetime", "time", "str", "int", "bool"]

#: Each value with no wire meaning, and the type the refusal names.
_NO_WIRE = [
    (_Order(1), "_Order"),
    (_Struct("x"), "_Struct"),
    (object(), "object"),
    ({"k": 1}, "dict"),
    ([["a"]], "list"),
    (_Ratio.HALF, "_Ratio"),
]
_NO_WIRE_IDS = ["dataclass", "struct", "object", "dict", "nested-list", "float-enum"]


def _client(sent: list[httpx2.Request]) -> Client:
    def handle(request: httpx2.Request) -> httpx2.Response:
        sent.append(request)
        return httpx2.Response(200)

    return Client(base_url="https://api.example", http=HttpOptions(transport=httpx2.MockTransport(handle)))


@pytest.mark.parametrize(("value", "text"), _WIRE, ids=_WIRE_IDS)
async def test_params_send_only_wire_values(value: object, text: str) -> None:
    """Send each value as what it means on the wire, never as its ``repr``."""
    sent: list[httpx2.Request] = []
    async with _client(sent) as client:
        await client.get("x", expect=nothing, params={"v": value})

    assert sent[0].url.params["v"] == text


@pytest.mark.parametrize(("value", "got"), _NO_WIRE, ids=_NO_WIRE_IDS)
async def test_params_refuse_a_value_with_no_wire_meaning(value: object, got: str) -> None:
    """Refuse the value before anything is sent, naming the key and its type."""
    sent: list[httpx2.Request] = []
    async with _client(sent) as client:
        with pytest.raises(TypeError, match=rf"^params= takes a str, .* \(got {got} under 'v'\)"):
            await client.get("x", expect=nothing, params={"v": value})

    assert sent == []


@pytest.mark.parametrize(("value", "text"), _WIRE, ids=_WIRE_IDS)
async def test_form_fields_send_only_wire_values(value: object, text: str) -> None:
    """Send a form field as the same allowlist encodes a query value."""
    sent: list[httpx2.Request] = []
    async with _client(sent) as client:
        await client.post("x", expect=nothing, body=Form({"v": value}))

    assert parse_qs(sent[0].content.decode()) == {"v": [text]}


@pytest.mark.parametrize(("value", "got"), _NO_WIRE, ids=_NO_WIRE_IDS)
def test_form_and_multipart_fields_refuse_a_value_with_no_wire_meaning(value: object, got: str) -> None:
    """Refuse the field when the body is built, naming the key and its type."""
    with pytest.raises(TypeError, match=rf"^Form\(fields=\) takes a str, .* \(got {got} under 'v'\)"):
        Form({"v": value})
    with pytest.raises(TypeError, match=rf"^Multipart\(fields=\) takes bytes, a str, .* \(got {got} under 'v'\)"):
        Multipart({"f": b"x"}, fields={"v": value})


async def test_multipart_fields_send_an_enum_as_its_value() -> None:
    """Send a multipart field through the same allowlist."""
    sent: list[httpx2.Request] = []
    async with _client(sent) as client:
        await client.post("x", expect=nothing, body=Multipart({"f": b"x"}, fields={"v": _Status.OPEN, "n": [1, 2]}))

    body = sent[0].content
    assert b'name="v"\r\n\r\nopen\r\n' in body
    assert body.count(b'name="n"') == len((1, 2))


async def test_multipart_fields_send_bytes_as_they_are() -> None:
    """Send a ``bytes`` multipart field raw, as httpx2 does: a part carries bytes, unlike a query or a form."""
    sent: list[httpx2.Request] = []
    async with _client(sent) as client:
        await client.post("x", expect=nothing, body=Multipart({"f": b"x"}, fields={"v": b"\x00raw"}))

    assert b'name="v"\r\n\r\n\x00raw\r\n' in sent[0].content


def test_form_fields_refuse_bytes() -> None:
    """Refuse ``bytes`` in a form field, which a form can carry only as text."""
    with pytest.raises(TypeError, match=r"^Form\(fields=\) takes a str, .* \(got bytes under 'v'\)"):
        Form({"v": b"raw"})


async def test_query_params_with_a_repeated_key_send_every_value() -> None:
    """Send every value of an ``httpx2.QueryParams`` built from pairs, where only the first of a repeated key went."""
    fake = FakeTransport().reply("GET", "/x", 200)
    params = httpx2.QueryParams([("a", "1"), ("a", "2"), ("b", "3")])
    async with Client(base_url="https://api.example.test/", http=HttpOptions(transport=fake)) as client:
        await client.get("x", expect=nothing, params=params)

    assert fake.calls[0].url.params.multi_items() == [("a", "1"), ("a", "2"), ("b", "3")]


async def test_a_walk_sends_every_value_of_its_query_params() -> None:
    """Send every value of a walk's ``httpx2.QueryParams`` on each page, as a call does."""
    fake = FakeTransport().reply("GET", "/x", httpx2.Response(200, json=[]))
    params = httpx2.QueryParams([("tag", "a"), ("tag", "b")])
    pager = LinkHeader(reader(list[int]), items=list)
    async with Client(base_url="https://api.example.test/", http=HttpOptions(transport=fake)) as client:
        await client.paginate("x", pager=pager, params=params).collect()

    assert fake.calls[0].url.params.get_list("tag") == ["a", "b"]
