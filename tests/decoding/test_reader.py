# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""``Reader``: a class around a function, so ``expect=`` refuses a model class, a type or a bare function."""

from __future__ import annotations

import functools
from typing import TYPE_CHECKING, Any, cast

import httpx2
import msgspec
import pytest

import headgate
import headgate.errors
import headgate.msgspec
import headgate.pydantic
from headgate import Client, Expect, HttpOptions, Reader, as_bytes, event_stream, nothing, reader_for
from headgate.cache import conditional
from headgate.msgspec import CODEC
from headgate.testing import FakeTransport

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from headgate.pagination import PageRequest


class Order(msgspec.Struct, frozen=True):
    """A decoded body."""

    id: int


def _plain(resp: httpx2.Response, /) -> int:
    """Return *resp*'s status: a reader's function, not a reader."""
    return resp.status_code


#: What a reader is mistaken for, each with how a refusal names it.
_NOT_READERS = pytest.mark.parametrize(
    ("value", "named"),
    [
        (str, "the class str"),
        (Order, "the class Order"),
        (list[Order], "the type list[Order]"),
        (_plain, "function"),
        (lambda resp: resp, "function"),
    ],
    ids=["str", "class", "type", "function", "lambda"],
)


def test_a_reader_calls_its_function_with_the_response() -> None:
    """Hand the response to the function the reader wraps, and return what it returns."""
    assert Reader(lambda r: r.status_code)(httpx2.Response(201)) == httpx2.codes.CREATED


def test_a_readers_repr_names_its_function() -> None:
    """Name a reader after its function, a partial after the function it fixes, or by the name it was given."""

    def orders(r: httpx2.Response, /, *, page: int = 0) -> int:
        return r.status_code + page

    assert repr(Reader(orders)) == "Reader(orders)"
    assert repr(Reader(orders, name="rows")) == "Reader(rows)"
    assert repr(Reader(functools.partial(orders, page=1))) == "Reader(orders)"


def test_a_reader_of_a_callable_without_a_name_is_named_by_its_type() -> None:
    """Name a reader over a callable object, which has no ``__name__``, after the object's class."""

    class Rows:
        def __call__(self, resp: httpx2.Response, /) -> int:
            return resp.status_code

    assert repr(Reader(Rows())) == "Reader(Rows)"


def test_a_codec_readers_repr_names_the_type() -> None:
    """Name a codec's reader after the codec's class and the type it decodes."""
    assert repr(reader_for(CODEC, list[Order])) == "Reader(MsgspecCodec list[Order])"


def test_the_old_builders_are_gone() -> None:
    """Export no old builder beside the new: ``reader_for``, ``writer_for`` and a flavor's ``reader`` and ``writer``."""
    for name in ("json_reader", "json_writer", "decoder_for", "encoder_for"):
        assert not hasattr(headgate, name), name
    for flavor in (headgate.msgspec, headgate.pydantic):
        for name in ("decoder", "encoder"):
            assert not hasattr(flavor, name), (flavor.__name__, name)
    for name in ("RefusalCheck", "RefusedError"):
        assert not hasattr(headgate, name), name
    assert hasattr(headgate, "ResponseCheck")
    assert headgate.errors.RefusedError.__module__ == "headgate.errors"


def test_stock_readers_are_readers() -> None:
    """Ship ``nothing``, ``as_bytes`` and ``event_stream`` as readers, each named after itself."""
    assert isinstance(nothing, Reader)
    assert isinstance(as_bytes, Reader)
    assert isinstance(event_stream, Reader)
    assert repr(nothing) == "Reader(nothing)"
    assert repr(as_bytes) == "Reader(as_bytes)"
    assert repr(event_stream) == "Reader(event_stream)"


@_NOT_READERS
@pytest.mark.parametrize(
    ("build", "option"),
    [
        (Expect, "ok= takes a Reader"),
        (lambda value: Expect(nothing, {404: value}), "answers= takes a Reader for each status"),
        (lambda value: Expect(nothing).on(404, value), "read= takes a Reader"),
        (conditional, "reader= takes a Reader"),
    ],
    ids=["ok", "answers", "on", "conditional"],
)
def test_expect_refuses_a_callable_that_is_not_a_reader(
    build: Callable[[Any], object], option: str, value: object, named: str
) -> None:
    """Refuse a class, a type or a bare function where a reader goes, naming the option and what it got."""
    with pytest.raises(TypeError) as caught:
        build(cast("Any", value))
    assert str(caught.value) == f"{option} (got {named})"


class _FirstRaises:
    """Just enough of a pager to fail when asked for its first page, so a walk that asks it fails."""

    def first(self, request: PageRequest[None]) -> PageRequest[None]:
        """Fail: ``paginate`` must check its ``expect=`` before it runs the pager."""
        del request
        msg = "the pager ran before expect= was checked"
        raise AssertionError(msg)


async def _paginate(client: Client, expect: Reader[object]) -> object:
    """Start a walk read through *expect*, whose pager fails if it is asked for a page."""
    return client.paginate("x", expect=expect, pages=cast("Any", _FirstRaises()))


#: Each call that takes ``expect=``, given one.
_CALLS: dict[str, Callable[[Client, Any], Awaitable[object]]] = {
    "get": lambda c, e: c.get("x", expect=e),
    "post": lambda c, e: c.post("x", expect=e),
    "put": lambda c, e: c.put("x", expect=e),
    "patch": lambda c, e: c.patch("x", expect=e),
    "delete": lambda c, e: c.delete("x", expect=e),
    "head": lambda c, e: c.head("x", expect=e),
    "paginate": _paginate,
}


@_NOT_READERS
@pytest.mark.parametrize("verb", list(_CALLS))
async def test_every_call_refuses_a_non_reader_expect(verb: str, value: object, named: str) -> None:
    """Refuse a non-reader ``expect=`` where the call is made, before the pager runs or anything is sent."""
    fake = FakeTransport()
    async with Client(base_url="https://api.test", http=HttpOptions(transport=fake)) as client:
        with pytest.raises(TypeError) as caught:
            await _CALLS[verb](client, cast("Any", value))
    assert str(caught.value) == f"expect= takes a Reader or an Expect (got {named})"
    assert not fake.calls
