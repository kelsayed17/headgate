# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""The declared-responses contract (``headgate._responses``): ``Expect``, ``RefusedError``, and the stock readers."""

from __future__ import annotations

import dataclasses
import traceback
from collections.abc import MutableMapping
from typing import Final, NoReturn, Protocol

import httpx2
import msgspec
import pytest

from headgate import Client, ClientError, HttpOptions, RateLimitError, UpstreamStatusError, _responses, body_reader
from headgate._responses import Expect, Reader, as_bytes, as_json, nothing
from headgate.errors import DecodeError, RefusedError, ResponseParseError
from headgate.msgspec import decode, reader
from headgate.testing import FakeTransport
from tests.support.responses import response


class _Ok(msgspec.Struct, frozen=True):
    x: int


class _No(msgspec.Struct, frozen=True):
    message: str | None = None


class _Env(msgspec.Struct, frozen=True):
    status: str
    x: int = 0


class _HasStatus(Protocol):
    @property
    def status(self) -> str:
        """Return the envelope's status."""
        ...


def _quota_check(env: _HasStatus) -> RefusedError | None:
    """Refuse every status but ``OK``; typed over a structural Protocol naming only the field it reads."""
    if env.status == "OK":
        return None
    return RefusedError(f"refused: {env.status}", code=env.status, kind=RateLimitError)


# --- readers ---


def test_reader_reads_a_body_through_the_response() -> None:
    """Decode the body of the response a reader is handed."""
    assert reader(_Ok)(response(b'{"x": 3}')) == _Ok(x=3)


def test_reader_is_strict_so_a_coerced_token_is_drift() -> None:
    """Reject a coerced token by default."""
    with pytest.raises(DecodeError):
        reader(_Ok)(response(b'{"x": "3"}'))


def test_allow_empty_body_reads_zero_bytes_as_the_empty_array() -> None:
    """Read an empty body as ``[]`` only when opted in."""
    assert reader(list[_Ok], allow_empty_body=True)(response(b"")) == []
    with pytest.raises(DecodeError):
        reader(list[_Ok])(response(b""))


def test_reader_reads_a_union_of_shapes() -> None:
    """Decode either arm of a union of shapes."""
    assert reader(list[_Ok] | _Ok)(response(b"[]")) == []
    assert reader(list[_Ok] | _Ok)(response(b'{"x": 1}')) == _Ok(x=1)


def test_a_check_that_refuses_raises_the_refused_it_returned() -> None:
    """Raise the ``RefusedError`` a check returns, with its code and kind."""
    read = reader(_Env, refuse=_quota_check)
    with pytest.raises(RefusedError) as ei:
        read(response(b'{"status": "QUOTA"}'))
    assert (ei.value.code, ei.value.kind) == ("QUOTA", RateLimitError)
    assert read(response(b'{"status": "OK", "x": 1}')) == _Env(status="OK", x=1)


def test_a_check_never_sees_a_body_that_did_not_decode() -> None:
    """Raise ``DecodeError`` before the check runs on a body that does not decode."""
    seen: list[_Env] = []

    def check(env: _Env) -> RefusedError | None:
        """Record every value the check is handed."""
        seen.append(env)
        return None

    with pytest.raises(DecodeError):
        reader(_Env, refuse=check)(response(b"not json"))
    assert seen == []


def _fixed_width(content: bytes) -> list[tuple[str, int]]:
    """Decode a fixed-width TXT file of a 4-character name then a count: a bytes decoder in no codec's format."""
    return [(line[:4].decode().strip(), int(line[4:])) for line in content.splitlines()]


def test_a_body_reader_hands_decode_the_body_bytes() -> None:
    """Return what *decode* makes of the body, whatever its format."""
    assert body_reader(_fixed_width)(response(b"ab    1\ncd   22")) == [("ab", 1), ("cd", 22)]


def test_a_body_reader_hands_decode_an_empty_body_as_it_is_by_default() -> None:
    """Substitute nothing for an empty body unless ``empty`` is given, so no format's empty value is assumed."""
    seen: list[bytes] = []
    body_reader(seen.append)(response(b" \n"))
    assert seen == [b" \n"]


def test_a_body_reader_reads_an_empty_body_as_empty_when_given() -> None:
    """Hand *decode* the bytes ``empty`` names for an empty body, and the body itself otherwise."""
    read = body_reader(_fixed_width, empty=b"")
    assert read(response(b"  \n ")) == []
    assert read(response(b"ab    1")) == [("ab", 1)]


def test_a_body_reader_refuses_with_refuse() -> None:
    """Run ``refuse`` on what *decode* returned, as a codec's reader does."""
    read = body_reader(lambda content: _Env(status=content.decode()), refuse=_quota_check)
    with pytest.raises(RefusedError, match="refused: QUOTA"):
        read(response(b"QUOTA"))
    assert read(response(b"OK")) == _Env(status="OK")


async def test_a_body_readers_decode_failure_is_a_parse_error() -> None:
    """Report a hand-written decoder's ``ValueError`` on a 2xx as ``ResponseParseError``, as any reader's."""
    fake = FakeTransport().reply("GET", "/rows", httpx2.Response(200, content=b"ab  one"))
    client = Client(base_url="https://api.test", http=HttpOptions(transport=fake))

    with pytest.raises(ResponseParseError) as raised:
        await client.get("rows", expect=body_reader(_fixed_width))
    assert raised.value.__cause__ is None


def _nested(depth: int, inner: bytes) -> bytes:
    """Return *inner* wrapped in *depth* JSON arrays."""
    return b"[" * depth + inner + b"]" * depth


#: Bodies ``as_json`` reads, each with the value it reads: a float and ``null`` (``parse_float``'s finite path), an
#: escaped surrogate pair, and a clean key over non-string values behind a surrogate escape (every branch of the walk).
_READ: Final = [
    (b'{"a": [1, 1.5, null]}', {"a": [1, 1.5, None]}),
    (b'["\\ud83d\\ude00"]', ["\U0001f600"]),
    (b'{"k": [1, null, "\\ud83d\\ude00"]}', {"k": [1, None, "\U0001f600"]}),
]

#: Bodies ``as_json`` refuses, each with the exact message of its ``DecodeError``.
_REFUSED: Final = [
    (b"", "not JSON at line 1, column 1"),
    (b"{", "not JSON at line 1, column 2"),
    (b"\xff", "not UTF-8 at byte 0"),
    (b"NaN", "not JSON: NaN, Infinity or a number out of range"),
    (b"\xef\xbb\xbf{}", "not JSON at line 1, column 1"),
    ("{}".encode("utf-16"), "not UTF-8 at byte 0"),
    (b"1e400", "not JSON: NaN, Infinity or a number out of range"),
    (b"9" * 5000, "not JSON: NaN, Infinity or a number out of range"),
    (b'["\\ud800"]', "not JSON: a string with a lone surrogate"),
    (b'{"\\udc00": 1}', "not JSON: a string with a lone surrogate"),
    (_nested(3000, b'"\\ud800"'), "not JSON: a string with a lone surrogate"),
]


def test_json_reads_builtins_without_a_codec() -> None:
    """Read JSON into builtins with the standard library, with surrogate pairs joined, deeper than the recursion limit.

    The surrogate walk is iterative, so a pair at the bottom of 3000 arrays is read, not a ``RecursionError``.
    """
    for body, value in _READ:
        assert as_json(httpx2.Response(200, content=body)) == value
    deep = as_json(httpx2.Response(200, content=_nested(3000, b'"\\ud83d\\ude00"')))
    for _ in range(3000):
        assert isinstance(deep, list)
        (deep,) = deep
    assert deep == "\U0001f600"
    assert repr(as_json) == "Reader(as_json)"


async def test_json_refuses_a_body_that_is_not_json() -> None:
    """Refuse what msgspec refuses, with a message naming the position and never the input, its cause not chained."""
    seen: list[str] = []
    for body, _ in _REFUSED:
        with pytest.raises(DecodeError) as raised:
            as_json(httpx2.Response(200, content=body))
        refusal = raised.value
        assert refusal.__cause__ is None
        assert refusal.__suppress_context__
        text = "".join(traceback.format_exception(refusal))
        for library in ("JSONDecodeError", "UnicodeDecodeError", "Expecting", "BOM", "codec", "Exceeds the limit"):
            assert library not in text, (body[:20], text)
        seen.append(str(refusal))
    assert seen == [message for _, message in _REFUSED]

    for body, _ in _REFUSED:
        fake = FakeTransport().reply("GET", "/x", httpx2.Response(200, content=body))
        with pytest.raises(ResponseParseError):
            await Client(base_url="https://api.test", http=HttpOptions(transport=fake)).get("x", expect=as_json)
        fake = FakeTransport().reply("GET", "/x", httpx2.Response(404, content=body))
        client = Client(base_url="https://api.test", http=HttpOptions(transport=fake))
        with pytest.raises(UpstreamStatusError) as status:
            await client.get("x", expect=Expect(nothing).on(404, as_json))
        assert status.value.status_code == httpx2.codes.NOT_FOUND


def _overflow(*_: object) -> NoReturn:
    """Raise what a parser raises for a body nested past its own stack.

    How deep that is depends on the platform: from Python 3.14, ``json.loads`` recurses until the real C stack runs
    out, so no body overflows everywhere. The tests raise the error instead of nesting a body to provoke it.
    """
    msg = "maximum recursion depth exceeded while decoding a JSON array from a unicode string"
    raise RecursionError(msg)


@pytest.fixture
def parser_overflows(monkeypatch: pytest.MonkeyPatch) -> bytes:
    """Make ``as_json``'s ``json.loads`` raise ``RecursionError`` part way through, returning a body that does it."""
    monkeypatch.setattr(_responses, "_finite", _overflow)
    return b"[[1.5]]"


async def _read_raises(status: int, body: bytes, expect: Reader[object] | Expect[object]) -> ClientError:
    """Return what a call raises reading *body* on *status* through *expect*."""
    fake = FakeTransport().reply("GET", "/x", httpx2.Response(status, content=body))
    client = Client(base_url="https://api.test", http=HttpOptions(transport=fake))
    with pytest.raises(ClientError) as raised:
        await client.get("x", expect=expect)
    return raised.value


def test_json_refuses_nesting_too_deep_for_the_parser(parser_overflows: bytes) -> None:
    """Raise ``DecodeError`` where ``json.loads`` overflows its stack, not a raw ``RecursionError``."""
    with pytest.raises(DecodeError) as raised:
        as_json(httpx2.Response(200, content=parser_overflows))
    assert str(raised.value) == "not JSON: nested too deeply"
    assert raised.value.__cause__ is None
    assert raised.value.__suppress_context__


async def test_nesting_too_deep_for_the_parser_is_a_shape_error(parser_overflows: bytes) -> None:
    """Raise ``ResponseParseError`` on a 2xx, and the status's error on a declared 404, whichever reader overflowed."""
    for read in (as_json, Reader(_overflow)):
        error = await _read_raises(httpx2.codes.OK, parser_overflows, read)
        assert isinstance(error, ResponseParseError), error
        assert str(error).endswith("nested too deeply"), error
    error = await _read_raises(httpx2.codes.NOT_FOUND, parser_overflows, Expect(nothing).on(404, Reader(_overflow)))
    assert isinstance(error, UpstreamStatusError), error
    assert error.status_code == httpx2.codes.NOT_FOUND


def test_as_bytes_hands_back_the_body_undecoded() -> None:
    """Return the body bytes as they arrived."""
    assert as_bytes(response(b"\x00raw")) == b"\x00raw"


def test_nothing_returns_none_whatever_the_body() -> None:
    """Return ``None`` and ignore the body."""
    assert nothing(response(b"a message nobody reads")) is None


def test_decode_is_the_bytes_level_primitive() -> None:
    """Decode bytes directly, including a union of shapes."""
    assert decode(b'{"x": 1}', _Ok) == _Ok(x=1)
    assert decode(b"[]", list[_Ok] | _Ok) == []
    with pytest.raises(DecodeError):
        decode(b"not json", _Ok)


# --- RefusedError ---


def test_refused_defaults_to_a_plain_upstream_refusal() -> None:
    """Default to no code and the plain ``UpstreamStatusError`` kind."""
    refused = RefusedError("the provider said no")
    assert (refused.message, refused.code, refused.kind) == ("the provider said no", None, UpstreamStatusError)
    assert str(refused) == "the provider said no"


# --- Expect ---


def test_an_expect_carries_a_success_reader_and_no_answers() -> None:
    """Carry the success reader and an empty set of answers."""
    expect = Expect(reader(_Ok))
    assert expect.ok(response(b'{"x": 2}')) == _Ok(x=2)
    assert dict(expect.answers) == {}


def test_on_declares_an_exact_status_and_keeps_the_success_reader() -> None:
    """Add the declared status and keep the success reader."""
    expect = Expect(reader(_Ok)).on(400, reader(_No))
    assert expect.ok(response(b'{"x": 2}')) == _Ok(x=2)
    assert expect.answers[400](response(b'{"message": "no"}', status=400)) == _No(message="no")
    assert set(expect.answers) == {400}


def test_on_returns_a_new_expect_and_leaves_the_original_alone() -> None:
    """Return a new ``Expect`` and leave the original's answers unchanged."""
    base = Expect(reader(_Ok))
    widened = base.on(404, nothing)
    assert dict(base.answers) == {}
    assert set(widened.answers) == {404}


@pytest.mark.parametrize("status", [401, 407, 429, 500, 502, 503, 599])
def test_on_refuses_a_status_that_means_we_could_not_ask(status: int) -> None:
    """Refuse 401, 407, 429 and every 5xx."""
    with pytest.raises(ValueError, match="could not ask"):
        Expect(reader(_Ok)).on(status, reader(_No))


@pytest.mark.parametrize("status", [0, 99, 600, 1000])
def test_on_refuses_a_status_outside_the_http_range(status: int) -> None:
    """Refuse a status outside 100-599."""
    with pytest.raises(ValueError, match="not an HTTP status"):
        Expect(reader(_Ok)).on(status, reader(_No))


@pytest.mark.parametrize("status", [401, 407, 429, 503])
def test_the_constructor_refuses_a_status_that_means_we_could_not_ask(status: int) -> None:
    """Refuse what ``.on()`` refuses: an ``Expect`` built with ``answers`` makes the same claim.

    Without it, ``Expect(reader(X), {401: ...})`` would hand a real 401 back as data instead of ``AuthError``.
    """
    with pytest.raises(ValueError, match=f"^{status} means we could not ask"):
        Expect(reader(_Ok), {status: nothing})


@pytest.mark.parametrize("status", [99, 600])
def test_the_constructor_refuses_a_status_outside_the_http_range(status: int) -> None:
    """Refuse a status outside 100-599 in ``answers``."""
    with pytest.raises(ValueError, match=f"^{status} is not an HTTP status"):
        Expect(reader(_Ok), {status: nothing})


def test_the_constructor_refuses_a_bad_status_beside_a_legal_one() -> None:
    """Check every key, not only the first: the 503 sits after a legal 404."""
    with pytest.raises(ValueError, match=r"^503 means we could not ask"):
        Expect(reader(_Ok), {404: nothing, 503: nothing})


@pytest.mark.parametrize("status", [401, 99])
def test_the_constructor_and_on_refuse_with_the_same_message(status: int) -> None:
    """Refuse with the same message on both paths."""
    with pytest.raises(ValueError, match=str(status)) as built:
        Expect(reader(_Ok), {status: nothing})
    with pytest.raises(ValueError, match=str(status)) as declared:
        Expect(reader(_Ok)).on(status, nothing)
    assert str(built.value) == str(declared.value)


def test_the_constructor_accepts_a_legal_answer() -> None:
    """Accept legal statuses and read them with their readers."""
    expect = Expect[_Ok | _No | None](reader(_Ok), {400: reader(_No), 404: nothing})
    assert set(expect.answers) == {400, 404}
    assert expect.answers[400](response(b'{"message": "no"}', status=400)) == _No(message="no")


def test_answers_are_read_only_even_when_the_constructor_was_handed_a_dict() -> None:
    """Copy the caller's dict as well as freezing it, so mutating it afterwards changes nothing."""
    handed = {404: nothing}
    expect = Expect(reader(_Ok), handed)
    assert not isinstance(expect.answers, MutableMapping)
    handed[400] = nothing
    assert set(expect.answers) == {404}


def test_the_default_answers_are_read_only() -> None:
    """Freeze the default answers too, so no two ``Expect`` objects share a mutable default."""
    assert not isinstance(Expect(reader(_Ok)).answers, MutableMapping)


def test_on_refuses_declaring_a_status_twice() -> None:
    """Refuse a status already declared."""
    with pytest.raises(ValueError, match="already declared"):
        Expect(reader(_Ok)).on(400, reader(_No)).on(400, nothing)


def test_403_404_and_an_exact_204_are_claimable() -> None:
    """Accept 403, 404 and an exact 204 as answers."""
    expect = Expect(reader(_Ok)).on(403, reader(_No)).on(404, nothing).on(204, nothing)
    assert set(expect.answers) == {403, 404, 204}


@pytest.mark.parametrize("name", [f.name for f in dataclasses.fields(Expect)])
def test_an_expect_refuses_assignment_to_every_field(name: str) -> None:
    """Refuse rebinding any field at runtime, not only in a type checker."""
    expect = Expect(reader(_Ok)).on(404, nothing)
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(expect, name, nothing)


def test_an_expect_is_not_a_reader() -> None:
    """Never be callable: that is what keeps a verb's ``Reader[T] | Expect[T]`` unambiguous."""
    expect = Expect(reader(_Ok)).on(404, nothing)
    assert not isinstance(expect.answers, MutableMapping)
    assert not callable(expect)


def test_the_lowest_status_may_be_declared() -> None:
    """Accept 100, the first HTTP status, as a declared answer; only below it is not a status."""
    lowest = httpx2.codes.CONTINUE
    assert lowest in Expect(nothing).on(lowest, nothing).answers
