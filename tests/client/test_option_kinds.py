# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""A call's options, a view's and a client's headers are checked for their kind where they are given.

An untyped caller (or one reading ``Any``-typed JSON) gets a ``TypeError`` naming the option, what it takes and what it
got, rather than an ``AttributeError`` from inside the client, httpx2's own words, or a value sent as it was. A set of
default headers names each header once, where a call's own headers are sent line by line.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final, cast

import httpx2
import pytest

from headgate import Client, Expect, HttpOptions, IdempotencyKeys, StaticHeaderAuth, nothing
from headgate.testing import FakeTransport

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

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


async def test_a_line_break_in_an_idempotency_key_or_an_event_id_is_refused_naming_its_option() -> None:
    """Refuse a key or an event id holding CR or LF where the call is written, naming the option, not the value."""
    client = _client()
    with pytest.raises(ValueError, match=r"^idempotency_key= takes header values with no CR, LF or NUL"):
        await client.post("x", expect=nothing, idempotency_key="k1\r\nX-Injected: yes")
    with pytest.raises(ValueError, match=r"^last_event_id= takes header values with no CR, LF or NUL"):
        client.events("x", last_event_id="e1\nX-Injected: yes")


@pytest.mark.parametrize("method", ["BREW", "get", "Post"])
def test_stream_refuses_a_method_it_does_not_send(method: str) -> None:
    """Refuse a method that is not one of the seven a client sends, in upper case, before anything is sent."""
    client = _client()
    with pytest.raises(ValueError, match=rf"^method= takes one of 'GET', 'POST', .*'QUERY' \(got '{method}'\)$"):
        client.stream("x", method=cast("Any", method))


@pytest.mark.parametrize("method", [5, None, b"GET"], ids=["int", "none", "bytes"])
def test_stream_refuses_a_method_that_is_not_text(method: object) -> None:
    """Refuse a method that is not a ``str`` at all with a ``TypeError``, as every option of the wrong kind."""
    client = _client()
    with pytest.raises(TypeError, match=rf"^method= takes one of 'GET', .*'QUERY' \(got {type(method).__name__}\)$"):
        client.stream("x", method=cast("Any", method))


#: A header named twice in one set of headers: by two lines of it, and by two keys that differ only in case.
_TWICE: Final[Mapping[str, Mapping[str, str]]] = {
    "two-lines": httpx2.Headers([("Cookie", "a=1"), ("Cookie", "b=2")]),
    "two-cases": {"Cookie": "a=1", "cookie": "b=2"},
}

#: Each place a set of default headers is given, built from the headers it is handed.
_DEFAULTS: Final[Mapping[str, Callable[[Mapping[str, str]], object]]] = {
    "HttpOptions": lambda headers: HttpOptions(headers=headers),
    "view": lambda headers: _client().with_options(headers=headers),
    "Expect": lambda headers: Expect(nothing, headers=headers),
    "StaticHeaderAuth": StaticHeaderAuth,
}


@pytest.mark.parametrize("headers", list(_TWICE.values()), ids=list(_TWICE))
@pytest.mark.parametrize("build", list(_DEFAULTS.values()), ids=list(_DEFAULTS))
def test_a_set_of_default_headers_names_each_header_once(
    build: Callable[[Mapping[str, str]], object], headers: Mapping[str, str]
) -> None:
    """Refuse a header named twice, in any case, where defaults are given: merged by name, it went out as one line.

    Its values were joined with ``", "``, which a server reads as one cookie for ``Cookie``.
    """
    with pytest.raises(
        ValueError,
        match=r"^headers= takes each header once \(got 'cookie' more than once\): give it once, .*'; '.*', '",
    ):
        build(headers)


#: The refusal of a header name that is not an HTTP token, after the option's name.
_NOT_A_NAME: Final = r"takes header names that are HTTP tokens, of letters, digits and !#\$%&'\*\+-\.\^_`\|~ \(got "
#: The refusal of a header value that would end the header, after the option's name; it shows no value.
_LINE_BREAK: Final = r"takes header values with no CR, LF or NUL \(got one under 'X-Tenant'\): "

#: Headers no request can carry, each with what its refusal says after the option's name: over curl, a line break in
#: a value, or one a name holds, ends the header there and starts another the caller never wrote.
_UNSENDABLE: Final[Mapping[str, tuple[Mapping[str, str], str]]] = {
    "name-space": ({"X Tenant": "t1"}, _NOT_A_NAME + r"'X Tenant'\)$"),
    "name-colon": ({"X-Tenant:": "t1"}, _NOT_A_NAME + r"'X-Tenant:'\)$"),
    "name-line-break": ({"X-Tenant\r\nX-Injected": "t1"}, _NOT_A_NAME + r"'X-Tenant\\r\\nX-Injected'\)$"),
    "name-empty": ({"": "t1"}, _NOT_A_NAME + r"''\)$"),
    "value-crlf": ({"X-Tenant": "t1\r\nX-Injected: yes"}, _LINE_BREAK),
    "value-lf": ({"X-Tenant": "t1\nX-Injected: yes"}, _LINE_BREAK),
    "value-nul": ({"X-Tenant": "t1\0"}, _LINE_BREAK),
}


@pytest.mark.parametrize(("headers", "message"), list(_UNSENDABLE.values()), ids=list(_UNSENDABLE))
@pytest.mark.parametrize("build", list(_DEFAULTS.values()), ids=list(_DEFAULTS))
def test_a_header_no_request_can_carry_is_refused_where_defaults_are_given(
    build: Callable[[Mapping[str, str]], object], headers: Mapping[str, str], message: str
) -> None:
    """Refuse a name that is no HTTP token, and a value holding CR, LF or NUL, naming the header, not the value."""
    with pytest.raises(ValueError, match=f"^headers= {message}") as refused:
        build(headers)

    assert "t1" not in str(refused.value)


@pytest.mark.parametrize(("headers", "message"), list(_UNSENDABLE.values()), ids=list(_UNSENDABLE))
async def test_a_header_no_request_can_carry_is_refused_on_a_call(headers: Mapping[str, str], message: str) -> None:
    """Refuse such a header on a call's own ``headers=``, before anything is sent."""
    fake = FakeTransport().reply("GET", "/x", 200)
    async with Client(base_url=_BASE, http=HttpOptions(transport=fake)) as client:
        with pytest.raises(ValueError, match=f"^headers= {message}"):
            await client.get("x", expect=nothing, headers=headers)

    assert fake.calls == ()


@pytest.mark.parametrize(
    ("header", "shown"),
    [("Idempotency Key", r"'Idempotency Key'"), ("Key\r\nX-Injected: yes", r"'Key\\r\\nX-Injected: yes'"), ("", "''")],
    ids=["space", "line-break", "empty"],
)
def test_an_idempotency_header_that_is_no_token_is_refused(header: str, shown: str) -> None:
    """Refuse an ``IdempotencyKeys(header=)`` that is no HTTP token where the policy is built."""
    with pytest.raises(ValueError, match=f"^header= {_NOT_A_NAME}{shown}\\)$"):
        IdempotencyKeys(header=header)


@pytest.mark.parametrize("on_view", [False, True], ids=["client", "view"])
async def test_a_calls_own_header_given_twice_is_sent_as_two_lines(*, on_view: bool) -> None:
    """Send each line of a header a call's own ``headers=`` give twice, as httpx2 does, on a client and on a view."""
    fake = FakeTransport().reply("GET", "/x", 200)
    client = Client(base_url=_BASE, http=HttpOptions(transport=fake))
    caller = client.with_options(headers={"X-View": "1"}) if on_view else client

    await caller.get("x", expect=nothing, headers=_TWICE["two-lines"])

    assert fake.calls[0].headers.get_list("cookie") == ["a=1", "b=2"]
