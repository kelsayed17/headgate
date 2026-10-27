# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""The declared-responses contract (``headgate._responses``): ``Expect``, ``RefusedError``, and the stock readers."""

from __future__ import annotations

import dataclasses
import json
from collections.abc import MutableMapping
from typing import Protocol

import httpx2
import msgspec
import pytest

from headgate._responses import Expect, RefusedError, as_bytes, json_reader, nothing
from headgate.errors import DecodeError, RateLimitError, UpstreamStatusError
from headgate.msgspec import decode, decoder
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


def test_decoder_reads_a_body_through_the_response() -> None:
    """Decode the body of the response a reader is handed."""
    assert decoder(_Ok)(response(b'{"x": 3}')) == _Ok(x=3)


def test_decoder_is_strict_so_a_coerced_token_is_drift() -> None:
    """Reject a coerced token by default."""
    with pytest.raises(DecodeError):
        decoder(_Ok)(response(b'{"x": "3"}'))


def test_allow_empty_body_reads_zero_bytes_as_the_empty_array() -> None:
    """Read an empty body as ``[]`` only when opted in."""
    assert decoder(list[_Ok], allow_empty_body=True)(response(b"")) == []
    with pytest.raises(DecodeError):
        decoder(list[_Ok])(response(b""))


def test_decoder_reads_a_union_of_shapes() -> None:
    """Decode either arm of a union of shapes."""
    assert decoder(list[_Ok] | _Ok)(response(b"[]")) == []
    assert decoder(list[_Ok] | _Ok)(response(b'{"x": 1}')) == _Ok(x=1)


def test_a_check_that_refuses_raises_the_refused_it_returned() -> None:
    """Raise the ``RefusedError`` a check returns, with its code and kind."""
    read = decoder(_Env, refusal_check=_quota_check)
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
        decoder(_Env, refusal_check=check)(response(b"not json"))
    assert seen == []


def test_a_json_reader_reads_an_empty_body_as_the_empty_array_when_allowed() -> None:
    """Hand *decode* the empty JSON array for an empty body, only when ``allow_empty_body`` is set."""
    assert json_reader(json.loads, allow_empty_body=True)(response(b"  ")) == []
    assert json_reader(json.loads, allow_empty_body=True)(response(b'[{"x": 1}]')) == [{"x": 1}]


def test_a_json_reader_raises_the_refused_its_check_returns() -> None:
    """Run ``refusal_check`` on what *decode* returned, as a codec's reader does."""
    read = json_reader(lambda content: _Env(**json.loads(content)), refusal_check=_quota_check)
    with pytest.raises(RefusedError, match="refused: QUOTA"):
        read(response(b'{"status": "QUOTA"}'))
    assert read(response(b'{"status": "OK"}')) == _Env(status="OK")


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
    expect = Expect(decoder(_Ok))
    assert expect.ok(response(b'{"x": 2}')) == _Ok(x=2)
    assert dict(expect.answers) == {}


def test_on_declares_an_exact_status_and_keeps_the_success_reader() -> None:
    """Add the declared status and keep the success reader."""
    expect = Expect(decoder(_Ok)).on(400, decoder(_No))
    assert expect.ok(response(b'{"x": 2}')) == _Ok(x=2)
    assert expect.answers[400](response(b'{"message": "no"}', status=400)) == _No(message="no")
    assert set(expect.answers) == {400}


def test_on_returns_a_new_expect_and_leaves_the_original_alone() -> None:
    """Return a new ``Expect`` and leave the original's answers unchanged."""
    base = Expect(decoder(_Ok))
    widened = base.on(404, nothing)
    assert dict(base.answers) == {}
    assert set(widened.answers) == {404}


@pytest.mark.parametrize("status", [401, 407, 429, 500, 502, 503, 599])
def test_on_refuses_a_status_that_means_we_could_not_ask(status: int) -> None:
    """Refuse 401, 407, 429 and every 5xx."""
    with pytest.raises(ValueError, match="could not ask"):
        Expect(decoder(_Ok)).on(status, decoder(_No))


@pytest.mark.parametrize("status", [0, 99, 600, 1000])
def test_on_refuses_a_status_outside_the_http_range(status: int) -> None:
    """Refuse a status outside 100-599."""
    with pytest.raises(ValueError, match="not an HTTP status"):
        Expect(decoder(_Ok)).on(status, decoder(_No))


@pytest.mark.parametrize("status", [401, 407, 429, 503])
def test_the_constructor_refuses_a_status_that_means_we_could_not_ask(status: int) -> None:
    """Refuse what ``.on()`` refuses: an ``Expect`` built with ``answers`` makes the same claim.

    Without it, ``Expect(decoder(X), {401: ...})`` would hand a real 401 back as data instead of ``AuthError``.
    """
    with pytest.raises(ValueError, match=f"^{status} means we could not ask"):
        Expect(decoder(_Ok), {status: nothing})


@pytest.mark.parametrize("status", [99, 600])
def test_the_constructor_refuses_a_status_outside_the_http_range(status: int) -> None:
    """Refuse a status outside 100-599 in ``answers``."""
    with pytest.raises(ValueError, match=f"^{status} is not an HTTP status"):
        Expect(decoder(_Ok), {status: nothing})


def test_the_constructor_refuses_a_bad_status_beside_a_legal_one() -> None:
    """Check every key, not only the first: the 503 sits after a legal 404."""
    with pytest.raises(ValueError, match=r"^503 means we could not ask"):
        Expect(decoder(_Ok), {404: nothing, 503: nothing})


@pytest.mark.parametrize("status", [401, 99])
def test_the_constructor_and_on_refuse_with_the_same_message(status: int) -> None:
    """Refuse with the same message on both paths."""
    with pytest.raises(ValueError, match=str(status)) as built:
        Expect(decoder(_Ok), {status: nothing})
    with pytest.raises(ValueError, match=str(status)) as declared:
        Expect(decoder(_Ok)).on(status, nothing)
    assert str(built.value) == str(declared.value)


def test_the_constructor_accepts_a_legal_answer() -> None:
    """Accept legal statuses and read them with their readers."""
    expect = Expect(decoder(_Ok), {400: decoder(_No), 404: nothing})
    assert set(expect.answers) == {400, 404}
    assert expect.answers[400](response(b'{"message": "no"}', status=400)) == _No(message="no")


def test_answers_are_read_only_even_when_the_constructor_was_handed_a_dict() -> None:
    """Copy the caller's dict as well as freezing it, so mutating it afterwards changes nothing."""
    handed = {404: nothing}
    expect = Expect(decoder(_Ok), handed)
    assert not isinstance(expect.answers, MutableMapping)
    handed[400] = nothing
    assert set(expect.answers) == {404}


def test_the_default_answers_are_read_only() -> None:
    """Freeze the default answers too, so no two ``Expect`` objects share a mutable default."""
    assert not isinstance(Expect(decoder(_Ok)).answers, MutableMapping)


def test_on_refuses_declaring_a_status_twice() -> None:
    """Refuse a status already declared."""
    with pytest.raises(ValueError, match="already declared"):
        Expect(decoder(_Ok)).on(400, decoder(_No)).on(400, nothing)


def test_403_404_and_an_exact_204_are_claimable() -> None:
    """Accept 403, 404 and an exact 204 as answers."""
    expect = Expect(decoder(_Ok)).on(403, decoder(_No)).on(404, nothing).on(204, nothing)
    assert set(expect.answers) == {403, 404, 204}


@pytest.mark.parametrize("name", [f.name for f in dataclasses.fields(Expect)])
def test_an_expect_refuses_assignment_to_every_field(name: str) -> None:
    """Refuse rebinding any field at runtime, not only in a type checker."""
    expect = Expect(decoder(_Ok)).on(404, nothing)
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(expect, name, nothing)


def test_an_expect_is_not_a_reader() -> None:
    """Never be callable: that is what keeps a verb's ``Reader[T] | Expect[T]`` unambiguous."""
    expect = Expect(decoder(_Ok)).on(404, nothing)
    assert not isinstance(expect.answers, MutableMapping)
    assert not callable(expect)


def test_the_lowest_status_may_be_declared() -> None:
    """Accept 100, the first HTTP status, as a declared answer; only below it is not a status."""
    lowest = httpx2.codes.CONTINUE
    assert lowest in Expect(nothing).on(lowest, nothing).answers
