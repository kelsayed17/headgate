# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""One ``body=``, whose value says what it is: each ``Body`` class is sent as it says, and nothing else is a body.

Whether a body is sent again on a retry, a redirect or a GOAWAY is pinned end to end in
``tests/retry/test_write_safety.py`` and ``tests/integration/test_replayable_body.py``; here, what each body reports.
"""

from __future__ import annotations

import importlib
import io
import math
import os
from typing import TYPE_CHECKING, Any, Final, cast

import pytest

from headgate import EncodedBody, Form, HttpOptions, Json, Multipart, Rate, Raw, TransportPolicy, nothing
from headgate.errors import UpstreamStatusError
from tests.support.clients import StubClient
from tests.support.wire import Script

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

    from headgate import Body, Method

_VERBS: Final[tuple[Method, ...]] = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD")


def _client(script: Script) -> StubClient:
    """Return a client over *script*."""
    return StubClient(
        base_url="https://api.example.test",
        rate=Rate.per_second(1000),
        policy=TransportPolicy(breaker=None),
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


async def test_a_json_value_json_cannot_carry_is_refused() -> None:
    """Refuse ``NaN`` in a ``Json`` body before sending, as httpx2's ``json=`` did, rather than send invalid JSON."""
    script = Script()

    with pytest.raises(ValueError, match="Out of range float values are not JSON compliant"):
        await _client(script).post("orders", expect=nothing, body=Json({"x": math.nan}))

    assert script.sent == []


@pytest.mark.parametrize(
    ("value", "got"),
    [(b"raw", "bytes"), ({"a": 1}, "dict"), ("text", "str")],
    ids=["bytes", "dict", "str"],
)
async def test_a_value_that_is_no_body_is_refused(value: object, got: str) -> None:
    """Refuse a ``body=`` that is not one of the ``Body`` classes, naming what it got, before sending."""
    script = Script()
    options: dict[str, Any] = {"body": value}

    with pytest.raises(TypeError) as raised:
        await _client(script).post("orders", expect=nothing, **options)

    assert str(raised.value) == f"body= takes a Body: Json, Form, Multipart, Raw or a writer's EncodedBody (got {got})"
    assert script.sent == []


def test_form_fields_must_be_a_mapping() -> None:
    """Refuse form fields given as a list of pairs, in ``Form`` and in ``Multipart``, when the body is built."""
    pairs = cast("Any", [("a", "1")])
    message = r"^fields= takes a mapping of field names to values \(got list\)$"
    with pytest.raises(TypeError, match=message):
        Form(pairs)
    with pytest.raises(TypeError, match=message):
        Multipart({"f": b"x"}, pairs)


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
