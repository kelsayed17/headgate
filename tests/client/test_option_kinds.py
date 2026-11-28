# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""A call's options, a view's and a client's headers are checked for their kind where they are given.

An untyped caller (or one reading ``Any``-typed JSON) gets a ``TypeError`` naming the option, what it takes and what it
got, rather than an ``AttributeError`` from inside the client, httpx2's own words, or a value sent as it was.
"""

from __future__ import annotations

from typing import Any, Final, cast

import pytest

from headgate import Client, Expect, HttpOptions, nothing
from headgate.testing import FakeTransport

_BASE: Final = "https://api.example.test/"


def _client() -> Client:
    """Return a client answering every path it is asked with an empty 200."""
    fake = FakeTransport().reply("GET", "/x", 200).reply("POST", "/x", 200)
    return Client(base_url=_BASE, http=HttpOptions(transport=fake))


_CALL_OPTIONS: Final[list[tuple[dict[str, Any], str]]] = [
    ({"params": [("a", 1)]}, r"params= takes a mapping of names to values.*\(got list\)"),
    ({"params": {1: "a"}}, r"params= takes a mapping of names to values.*\(got the key 1, of type int\)"),
    ({"headers": [("X", "1")]}, r"headers= takes a mapping of header names to str values.*\(got list\)"),
    ({"headers": {"X-N": 1}}, r"headers= takes a mapping of header names to str values.*\(got int under 'X-N'\)"),
    ({"idempotency_key": 123}, r"idempotency_key= takes a key, a str.*\(got int\)"),
    ({"idempotency_key": None}, r"idempotency_key= takes a key, a str.*\(got NoneType\)"),
]


@pytest.mark.parametrize(
    ("options", "message"),
    _CALL_OPTIONS,
    ids=["pairs", "int-key", "header-pairs", "int-value", "int-key-id", "none-key"],
)
async def test_a_call_refuses_an_option_of_the_wrong_kind(options: dict[str, Any], message: str) -> None:
    """Refuse a call's option of the wrong kind before anything is sent, naming it."""
    async with _client() as client:
        with pytest.raises(TypeError, match=message):
            await client.post("x", expect=nothing, **options)


def test_a_view_refuses_headers_of_the_wrong_kind_where_it_is_made() -> None:
    """Refuse a view's headers given as pairs in ``with_options``, not at the view's first call."""
    client = _client()
    with pytest.raises(TypeError, match=r"headers= takes a mapping of header names to str values"):
        client.with_options(**cast("dict[str, Any]", {"headers": [("X", "1")]}))


@pytest.mark.parametrize("headers", [[("X", "1")], {"X-N": 1}], ids=["pairs", "int-value"])
def test_http_options_refuse_headers_of_the_wrong_kind(headers: object) -> None:
    """Refuse a client's headers given as pairs or with a value that is not a ``str``."""
    with pytest.raises(TypeError, match=r"headers= takes a mapping of header names to str values"):
        HttpOptions(headers=cast("Any", headers))


def test_an_expect_refuses_headers_of_the_wrong_kind() -> None:
    """Refuse an ``Expect``'s request headers whose value is not a ``str``."""
    with pytest.raises(TypeError, match=r"headers= takes a mapping of header names to str values"):
        Expect(nothing, headers=cast("Any", {"If-Match": 1}))


async def test_events_refuses_a_last_event_id_that_is_not_text() -> None:
    """Refuse a ``last_event_id`` that is neither a ``str`` nor ``None``, which httpx2 could not encode."""
    client = _client()
    with pytest.raises(TypeError, match=r"last_event_id= takes the id of the last event seen, a str, or None"):
        client.events("x", last_event_id=cast("Any", 5))


@pytest.mark.parametrize("method", ["BREW", "get", "Post"])
def test_stream_refuses_a_method_it_does_not_send(method: str) -> None:
    """Refuse a method that is not one of the seven a client sends, in upper case, before anything is sent."""
    client = _client()
    with pytest.raises(TypeError, match=r"method= takes one of 'GET', 'POST', .*'QUERY' \(got '"):
        client.stream("x", method=cast("Any", method))
