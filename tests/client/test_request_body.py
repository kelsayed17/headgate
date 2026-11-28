# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""One ``body=``, whose value says what it is: each ``Body`` class is sent as it says, and nothing else is a body.

Whether a body is sent again on a retry, a redirect or a GOAWAY is pinned end to end in
``tests/retry/test_write_safety.py`` and ``tests/integration/test_replayable_body.py``; here, what each body reports.
"""

from __future__ import annotations

import collections
import copy
import dataclasses
import importlib
import inspect
import io
import json
import math
import os
import pathlib
import pickle
import re
import tempfile
import types
from dataclasses import dataclass
from datetime import date
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Final, cast

import attrs
import httpx2
import msgspec
import pydantic
import pytest

from headgate import (
    CallPolicy,
    Client,
    EncodedBody,
    Form,
    HttpOptions,
    Json,
    Multipart,
    Rate,
    Raw,
    UpstreamStatusError,
    nothing,
)
from headgate._request_body import lowered
from tests.support.wire import Script

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

    from headgate import Body, Method

#: The verbs that take a body; ``get()`` and ``head()`` refuse every body keyword (see ``test_safe_reads.py``).
_VERBS: Final[tuple[Method, ...]] = ("POST", "PUT", "PATCH", "DELETE", "QUERY")


def _client(script: Script) -> Client:
    """Return a client over *script*."""
    return Client(
        base_url="https://api.example.test",
        policy=CallPolicy(rate=Rate.per_second(1000), breaker=None),
        http=HttpOptions(transport=script.transport()),
    )


async def _chunks() -> AsyncIterator[bytes]:
    """Yield a body in two chunks."""
    yield b"ab"
    yield b"c"


@pytest.mark.parametrize(
    ("body", "content", "content_type"),
    [
        (EncodedBody(b"<order/>", "application/xml"), b"<order/>", "application/xml"),
        (Json({"sku": "A-1", "qty": 2}), b'{"sku":"A-1","qty":2}', "application/json"),
        (Json(None), b"null", "application/json"),
        (Json("é"), '"é"'.encode(), "application/json"),
        (Form({"kind": "q", "n": 3}), b"kind=q&n=3", "application/x-www-form-urlencoded"),
        (Raw(b"raw bytes"), b"raw bytes", None),
        (Raw(_chunks), b"abc", None),
    ],
    ids=["encoded", "json", "json-null", "json-unicode", "form", "raw-bytes", "raw-factory"],
)
async def test_each_body_is_sent_as_it_says(body: Body, content: bytes, content_type: str | None) -> None:
    """Send each body's bytes with its ``Content-Type``; ``Json`` encodes as httpx2's ``json=`` did."""
    script = Script()

    await _client(script).post("orders", expect=nothing, body=body)

    sent = script.sent[0]
    assert (sent.content, sent.headers.get("content-type")) == (content, content_type)


async def test_a_multipart_body_sends_its_files_and_fields() -> None:
    """Send a ``Multipart`` body's file part and form fields in one ``multipart/form-data`` request."""
    script = Script()

    await _client(script).post(
        "reports", expect=nothing, body=Multipart({"report": ("q3.csv", b"a,b", "text/csv")}, {"kind": "q"})
    )

    sent = script.sent[0]
    assert sent.headers["content-type"].startswith("multipart/form-data; boundary=")
    assert b'name="report"; filename="q3.csv"' in sent.content
    assert b'name="kind"\r\n\r\nq' in sent.content


async def test_no_body_is_sent_for_none() -> None:
    """Send no body for ``body=None``, as for no ``body=`` at all, so a wrapper can forward an optional body."""
    script = Script()

    await _client(script).post("orders", expect=nothing, body=None)

    assert (script.sent[0].content, script.sent[0].headers.get("content-type")) == (b"", None)


async def test_a_json_content_type_yields_to_the_calls_headers() -> None:
    """Send the call's own ``Content-Type`` in place of the one ``Json`` stamps, as ``EncodedBody``'s does."""
    script = Script()

    await _client(script).post(
        "orders", expect=nothing, body=Json({"a": 1}), headers={"Content-Type": "application/merge-patch+json"}
    )

    assert script.sent[0].headers["content-type"] == "application/merge-patch+json"


@pytest.mark.parametrize("number", [math.nan, math.inf], ids=["nan", "inf"])
def test_a_json_value_json_cannot_carry_is_refused_where_it_is_built(number: float) -> None:
    """Refuse ``NaN`` or an infinity where the body is built, naming ``Json`` and what to write, not send bad JSON."""
    with pytest.raises(ValueError, match=r"^Json takes a value JSON can carry \(Out of range float values .*\): write"):
        Json({"x": number})


def test_a_json_body_is_encoded_once_where_it_is_built() -> None:
    """Send the bytes encoded where the body was built: a change to the value afterwards changes nothing sent."""
    value = {"qty": 1}
    body = Json(value)
    value["qty"] = 2

    def encoded_again(obj: object, **options: object) -> str:
        raise AssertionError((obj, options))

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(json, "dumps", encoded_again)
        sent = lowered(body)

    assert sent.content == b'{"qty":1}'


def test_a_json_body_is_a_value_of_its_value_alone() -> None:
    """Show only its value: no other field, a match on ``value``, ``copy.replace`` and no attribute to set."""
    body = Json({"a": 1})

    match body:
        case Json(value):
            assert value == {"a": 1}
    assert copy.replace(body, value=[1]) == Json([1])
    assert not dataclasses.is_dataclass(body)
    assert isinstance(inspect.getattr_static(Json, "value"), property), "value is a read-only property"
    with pytest.raises(AttributeError, match=r"^Json is immutable"):
        cast("Any", body).value = 2
    with pytest.raises(AttributeError, match=r"^Json is immutable"):
        del cast("Any", body).value


def test_a_json_body_copies_deep_copies_and_pickles_as_its_value() -> None:
    """Copy, deep-copy and pickle a ``Json``, as the dataclass of 0.12.1 did: each equal, and sending the same bytes.

    Each is built afresh from the value, by the reduce protocol ``pickle`` and ``copy.deepcopy`` both replay, so a deep
    copy holds a copy of it, and nothing reads or names its encoding.
    """
    body = Json({"a": [1], "name": "naïve"})
    copies = [copy.copy(body), copy.deepcopy(body)]

    assert copies == [body, body]
    assert [lowered(copied).content for copied in copies] == [lowered(body).content] * len(copies)
    assert copies[1].value is not body.value
    assert body.__reduce__() == (Json, (body.value,))
    assert pickle.dumps(body)


def test_a_json_body_refuses_a_change_without_naming_its_encoding() -> None:
    """Refuse setting or deleting any attribute in words that name no private slot, its encoding above all."""
    body = cast("Any", Json({"a": 1}))
    for name in ("value", "_encoded"):
        with pytest.raises(AttributeError) as refused:
            setattr(body, name, 2)
        assert str(refused.value) == "Json is immutable: build a new one, or copy.replace(body, value=...)"
        with pytest.raises(AttributeError) as refused:
            delattr(body, name)
        assert str(refused.value) == "Json is immutable: build a new one, or copy.replace(body, value=...)"


@pytest.mark.parametrize("value", ["\ud800", {"note": "a\udfffb"}], ids=["str", "nested"])
def test_a_json_value_utf8_cannot_encode_is_refused_naming_it(value: object) -> None:
    """Refuse text holding a lone surrogate where the body is built, naming the surrogate, not a raw encoder error."""
    with pytest.raises(ValueError, match=r"^Json takes a value JSON can carry \(got the lone surrogate '\\ud[8f]"):
        Json(value)


def test_a_json_body_cannot_be_subclassed() -> None:
    """Refuse a subclass, whose overrides nothing would call: the body is its encoded value."""
    with pytest.raises(TypeError, match=r"^_Mine cannot subclass Json, which is final"):

        class _Mine(cast("Any", Json)):
            pass


@dataclass(frozen=True)
class _Order:
    """A value of a class JSON's encoder does not know."""

    qty: int


@pytest.mark.parametrize(
    ("value", "got"),
    [(_Order(1), "_Order"), ({"due": date(2026, 1, 1)}, "date"), ({"ids": {1, 2}}, "set")],
    ids=["dataclass", "date", "set"],
)
def test_a_json_body_of_what_is_not_builtins_is_refused_where_it_is_built(value: object, got: str) -> None:
    """Refuse a value JSON cannot encode where the ``Json`` is built, naming its type and what to write instead."""
    with pytest.raises(TypeError, match=rf"^Json takes builtins.*\(got {got}\): .*writer\(.*to_builtins\(value\)"):
        Json(value)


def test_a_json_body_of_a_mapping_that_is_no_dict_is_told_to_copy_it() -> None:
    """Advise ``dict(value)`` for a mapping JSON does not encode, which is a dict away from builtins."""
    with pytest.raises(TypeError, match=r"\(got mappingproxy\): write dict\(value\) for a mapping that is not a dict$"):
        Json(MappingProxyType({"a": 1}))


@pytest.mark.parametrize(("value", "sent"), [((1, 2), b"[1,2]"), ({1: "a"}, b'{"1":"a"}')], ids=["tuple", "int-key"])
async def test_a_json_body_takes_what_the_json_encoder_takes(value: object, sent: bytes) -> None:
    """Send a tuple as an array and an ``int`` key as a string, as the standard library's encoder does."""
    script = Script()
    await _client(script).post("orders", expect=nothing, body=Json(value))

    assert script.sent[0].content == sent


def test_a_json_key_json_cannot_write_is_refused_where_it_is_built() -> None:
    """Refuse a key JSON cannot write, such as a tuple, in the same words, saying which keys it can."""
    with pytest.raises(
        TypeError, match=r"^Json takes builtins.*\(keys must be str, int, float, bool or None, not tuple\)"
    ):
        Json({(1, 2): 1})


def test_two_json_bodies_of_one_value_are_equal() -> None:
    """Compare and hash a ``Json`` by its value, whatever it holds besides."""
    assert Json({"a": 1}) == Json({"a": 1})
    assert hash(Json((1, "a"))) == hash(Json((1, "a")))
    assert repr(Json({"a": 1})) == "Json(value={'a': 1})"


@pytest.mark.parametrize(
    ("value", "got", "hint"),
    [
        (b"raw", "bytes", ": write body=Raw(value)"),
        (httpx2.ByteStream(b"raw"), "ByteStream", ": write body=Raw(value)"),
        (io.BytesIO(b"raw"), "BytesIO", ": write body=Raw(value.read())"),
        ({"a": 1}, "dict", ": write body=Json(value)"),
        (collections.OrderedDict(a=1), "OrderedDict", ": write body=Json(value)"),
        (types.MappingProxyType({"a": 1}), "mappingproxy", ": write body=Json(dict(value))"),
        ("text", "str", ""),
        (pathlib.Path("upload.bin"), type(pathlib.Path()).__name__, ""),
        (msgspec.Struct, "the class Struct", ""),
    ],
    ids=["bytes", "stream", "file", "dict", "ordered-dict", "mapping", "str", "path", "model-class"],
)
async def test_a_value_that_is_no_body_is_refused(value: object, got: str, hint: str) -> None:
    """Refuse a ``body=`` that is not one of the ``Body`` classes before sending, naming what it got and what to write.

    Raw holds bytes, a stream and a file's bytes, Json a mapping; anything else gets no advice it could not take.
    """
    script = Script()
    options: dict[str, Any] = {"body": value}

    with pytest.raises(TypeError) as raised:
        await _client(script).post("orders", expect=nothing, **options)

    assert str(raised.value) == (
        f"body= takes a Body: Json, Form, Multipart, Raw or a writer's EncodedBody (got {got}){hint}"
    )
    assert script.sent == []


@dataclass(frozen=True)
class _Point:
    """A typed value, such as a model, that a writer encodes."""

    x: int


class _Struct(msgspec.Struct):
    """A msgspec model."""

    x: int


class _Model(pydantic.BaseModel):
    """A pydantic model."""

    x: int


@attrs.define
class _Attrs:
    """An attrs class."""

    x: int


@pytest.mark.parametrize(
    "value", [_Point(1), _Struct(1), _Model(x=1), _Attrs(1)], ids=["dataclass", "msgspec", "pydantic", "attrs"]
)
async def test_a_typed_value_given_as_a_body_is_told_to_use_a_writer(value: object) -> None:
    """Refuse a model's instance as ``body=``, saying to encode it with a writer of its type."""
    script = Script()
    options: dict[str, Any] = {"body": value}

    with pytest.raises(TypeError) as raised:
        await _client(script).post("orders", expect=nothing, **options)

    name = type(value).__name__
    assert str(raised.value) == (
        f"body= takes a Body: Json, Form, Multipart, Raw or a writer's EncodedBody (got {name}): write "
        f"body=writer({name})(value) with a flavor's writer, or body=Json(to_builtins(value)) with its to_builtins"
    )
    assert script.sent == []


def test_form_fields_must_be_a_mapping() -> None:
    """Refuse form fields given as a list of pairs, in ``Form`` and in ``Multipart``, when the body is built."""
    pairs = cast("Any", [("a", "1")])
    message = r"^fields= takes a mapping of field names to values \(got list\)$"
    with pytest.raises(TypeError, match=message):
        Form(pairs)
    with pytest.raises(TypeError, match=message):
        Multipart({"f": b"x"}, pairs)


#: Files of none of the shapes an ``UploadFile`` takes, each with what the refusal says it got.
_NOT_FILES: Final[list[tuple[object, str]]] = [
    (5, "int"),
    ([b"x"], "list"),
    ((), "a tuple of nothing"),
    (io.StringIO("x"), "StringIO"),
    (("a.txt",), "a tuple of str"),
    (("a.txt", b"x", "text/plain", {}, "more"), "a tuple of str, bytes, str, dict, str"),
    ((5, b"x"), "a tuple of int, bytes"),
    (("a.txt", 5), "a tuple of str, int"),
    (("a.txt", b"x", 5), "a tuple of str, bytes, int"),
    (("a.txt", b"x", "text/plain", [("X-Part", "1")]), "a tuple of str, bytes, str, list"),
    (("a.txt", b"x", "text/plain", {"X-Part": 1}), "a tuple of str, bytes, str, dict"),
]


@pytest.mark.parametrize("as_pairs", [False, True], ids=["mapping", "pairs"])
@pytest.mark.parametrize(
    ("file", "got"),
    _NOT_FILES,
    ids=[
        "int",
        "list",
        "empty-tuple",
        "text-file",
        "one-item",
        "five-items",
        "int-filename",
        "int-file",
        "int-content-type",
        "header-pairs",
        "int-header-value",
    ],
)
def test_a_file_of_no_upload_shape_is_refused_where_it_is_written(file: object, got: str, *, as_pairs: bool) -> None:
    """Refuse a file of none of an ``UploadFile``'s shapes where it is written, naming its field, not at the send."""
    files = [("report", file)] if as_pairs else {"report": file}
    with pytest.raises(
        TypeError, match=rf"^files= takes a file for each field: .* \(got {re.escape(got)} under 'report'\)$"
    ):
        Multipart(cast("Any", files))


@pytest.mark.parametrize("files", [{5: b"x"}, [(5, b"x")]], ids=["mapping", "pairs"])
def test_a_file_field_named_by_no_str_is_refused(files: object) -> None:
    """Refuse a field name that is not a ``str``, which httpx2 could not write into the part's head."""
    with pytest.raises(
        TypeError, match=r"^files= takes a mapping of field names to files, .* \(got the key 5, of type int\)$"
    ):
        Multipart(cast("Any", files))


async def test_every_shape_of_an_upload_file_is_sent() -> None:
    """Send each shape an ``UploadFile`` names: the file alone, as bytes, text or any binary file, or in a tuple."""
    script = Script()
    # A named temporary file is no io.IOBase: a file is told by its read method, as httpx2 reads it.
    with tempfile.NamedTemporaryFile() as named:
        named.write(b"named")
        named.flush()
        files = {
            "bytes": b"x",
            "text": "x",
            "file": io.BytesIO(b"x"),
            "named": named,
            "pair": (None, b"x"),
            "typed": ("a.csv", "a,b", None),
            "headed": ("a.bin", io.BytesIO(b"x"), "application/octet-stream", {"X-Part": "1"}),
        }
        await _client(script).post("upload", expect=nothing, body=Multipart(files))

    sent = script.sent[0].content
    assert all(f'name="{name}"'.encode() in sent for name in files)
    assert b"named" in sent
    assert b"X-Part: 1" in sent


#: Each body keyword this phase removed, a value for it, and what the refusal says to write.
_OLD_BODIES: Final[list[tuple[str, object, str]]] = [
    ("json", {"a": 1}, "write body=Json(value)"),
    ("data", {"a": "1"}, "write body=Form(fields), or body=Multipart(files, fields) with files"),
    ("content", b"x", "write body=Raw(content)"),
    ("files", {"f": b"x"}, "write body=Multipart(files, fields)"),
]


@pytest.mark.parametrize(("name", "value", "hint"), _OLD_BODIES, ids=[name for name, _, _ in _OLD_BODIES])
@pytest.mark.parametrize("method", _VERBS)
async def test_every_verb_refuses_an_old_body_keyword(method: Method, name: str, value: object, hint: str) -> None:
    """Refuse ``json=``, ``data=``, ``content=`` and ``files=`` with what to write, rather than send no body."""
    script = Script()
    verb = getattr(_client(script), method.lower())

    with pytest.raises(TypeError) as raised:
        await verb("orders", expect=nothing, **{name: value})

    assert str(raised.value) == f"{method.lower()}() got an unexpected keyword argument {name!r}: {hint}"
    assert script.sent == []


@pytest.mark.parametrize(("name", "value", "hint"), _OLD_BODIES, ids=[name for name, _, _ in _OLD_BODIES])
def test_a_stream_refuses_an_old_body_keyword(name: str, value: object, hint: str) -> None:
    """Refuse the same keywords on ``stream()``, when it is called."""
    with pytest.raises(TypeError) as raised:
        _client(Script()).stream("orders", method="POST", **cast("dict[str, Any]", {name: value}))

    assert str(raised.value) == f"stream() got an unexpected keyword argument {name!r}: {hint}"


async def _one_shot() -> AsyncIterator[bytes]:
    """Yield a body once: a stream that cannot be read again."""
    yield b"once"


@pytest.fixture
def pipe() -> Iterator[io.BufferedReader]:
    """Provide the read end of a pipe, a file object that cannot be rewound."""
    read, write = os.pipe()
    os.close(write)
    with os.fdopen(read, "rb") as reader:
        yield reader


def test_a_raw_body_says_whether_it_can_be_sent_again() -> None:
    """Report bytes and a factory as replayable, and an async iterable, consumed by the first send, as not."""
    assert Raw(b"x").replayable
    assert Raw(_chunks).replayable
    assert not Raw(_one_shot()).replayable


def test_a_multipart_body_says_whether_it_can_be_sent_again(pipe: io.BufferedReader) -> None:
    """Report bytes and an in-memory file as replayable, and a file that cannot be rewound, such as a pipe, as not."""
    assert Multipart({"f": b"x"}).replayable
    assert Multipart({"f": ("a.txt", io.BytesIO(b"x"), "text/plain")}).replayable
    assert not Multipart({"f": pipe}).replayable


class _ReadOnly:
    """A binary file object with a ``read`` and nothing else, which httpx2 reads in chunks: no descriptor, no seek."""

    def __init__(self, data: bytes) -> None:
        """Hold *data* to read once."""
        self._data = io.BytesIO(data)

    def read(self, size: int = -1) -> bytes:
        """Return up to *size* bytes of what is left."""
        return self._data.read(size)


class _SaysItSeeks(_ReadOnly):
    """A file object that says it can seek, but has no ``seek`` to rewind it with."""

    def seekable(self) -> bool:
        """Say it can seek."""
        return True


async def test_a_file_object_with_only_a_read_is_sent_once() -> None:
    """Send a file object that has only its ``read``, which ``Multipart`` takes, as one that cannot be rewound.

    One that says it can seek but has no ``seek`` cannot be rewound either: httpx2 rewinds with ``seek``.
    """
    script = Script()
    body = Multipart({"f": ("a.bin", cast("Any", _ReadOnly(b"only-read")))})

    assert not body.replayable
    assert not Multipart({"f": cast("Any", _SaysItSeeks(b"x"))}).replayable
    await _client(script).post("upload", expect=nothing, body=body)

    assert b"only-read" in script.sent[0].content


def test_the_old_body_check_is_gone() -> None:
    """Remove ``check_one_body``: one ``body=`` cannot conflict with another."""
    module = importlib.import_module("headgate._request_body")
    assert not hasattr(module, "check_one_body")
    assert not hasattr(module, "request_content")


async def test_a_one_shot_raw_body_is_sent_once_and_bytes_are_sent_again() -> None:
    """Send a ``Raw`` async iterable once on a keyed POST's retryable 503, where the same body as bytes is re-sent."""
    once, twice = Script(503, 200), Script(503, 200)

    with pytest.raises(UpstreamStatusError):
        await _client(once).post("orders", expect=nothing, body=Raw(_one_shot()), idempotency_key="k")
    await _client(twice).post("orders", expect=nothing, body=Raw(b"once"), idempotency_key="k")

    assert (len(once.sent), [request.content for request in twice.sent]) == (1, [b"once", b"once"])


def test_a_multipart_body_needs_a_file() -> None:
    """Refuse a ``Multipart`` with no files, which httpx2 would send as a form: fields alone are a ``Form``."""
    with pytest.raises(
        ValueError, match=r"^Multipart needs at least one file; send form fields alone as Form\(fields\)$"
    ):
        Multipart({}, {"a": "1"})
