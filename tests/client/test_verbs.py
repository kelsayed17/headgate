# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""The verb methods on ``AsyncBaseClient``: get/post/put/patch/delete/head.

A recording ``MockTransport`` captures every outgoing ``httpx2.Request``, so each test asserts the verb maps to the
right HTTP method and threads its arguments through. Every verb takes the call's declared responses as ``expect=``
(a bare :data:`~headgate._responses.Reader` or an :class:`~headgate._responses.Expect`), and an undeclared non-2xx
always raises.

t-string paths are rendered once, while the request is built, so everything after (the send, the error path's
``sanitize_url``) sees a plain ``str``. A ``str`` path is trusted structure and reaches the wire verbatim, which is what
lets a caller forward an absolute cursor URL unescaped. Those tests assert on ``raw_path``, not ``url.path``: httpx2
percent-decodes ``path``, so ``BTC%2FUSD`` and ``BTC/USD`` read back alike, and only ``raw_path`` is what goes on the
wire.
"""

from __future__ import annotations

import httpx2
import msgspec
import pytest

from headgate import HttpOptions, TransportPolicy
from headgate import msgspec as flavor
from headgate._client import AsyncBaseClient
from headgate._responses import as_bytes, nothing
from headgate.errors import AuthError, UpstreamStatusError
from headgate.msgspec import decoder
from headgate.ratelimit import Rate
from headgate.retry import RetryPolicy
from tests.support.responses import RESPONSE

_VALUE = 7


class _Model(msgspec.Struct):
    """A success body."""

    value: int


class _Body(msgspec.Struct):
    """A request body."""

    name: str


class _Wire:
    """A transport handler that records every request and answers each with the same status and JSON body."""

    def __init__(self, status: int = httpx2.codes.OK, body: object = None) -> None:
        """Answer with *status* and, unless it is ``None``, *body* as JSON."""
        self.status = status
        self.body = body
        self.sent: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        """Record *request* and answer it."""
        self.sent.append(request)
        if self.body is None:
            return httpx2.Response(self.status)
        return httpx2.Response(self.status, json=self.body)

    @property
    def last(self) -> httpx2.Request:
        """The most recent request."""
        return self.sent[-1]


def _client(wire: _Wire) -> AsyncBaseClient:
    """Return an ``AsyncBaseClient`` whose network is *wire*, sending each request once."""
    return AsyncBaseClient(
        base_url="https://example.test",
        rate=Rate.per_second(2),
        policy=TransportPolicy(retry=RetryPolicy(attempts=1)),
        http=HttpOptions(transport=httpx2.MockTransport(wire)),
    )


async def test_get_uses_get_method_and_decodes_model() -> None:
    """Check that ``get`` sends a GET with its params and decodes the body."""
    wire = _Wire(body={"value": _VALUE})
    async with _client(wire) as c:
        out = await c.get("thing", expect=decoder(_Model), params={"a": "1"})

    assert wire.last.method == "GET"
    assert str(wire.last.url).endswith("/thing?a=1")
    assert out == _Model(value=_VALUE)


async def test_get_decodes_struct_and_json_and_the_whole_response() -> None:
    """Check that one ``get`` decodes into a struct, a plain mapping, or hands back the whole response."""
    async with _client(_Wire(body={"value": _VALUE})) as c:
        assert (await c.get("t", expect=decoder(_Model))).value == _VALUE
        assert await c.get("t", expect=decoder(dict[str, int])) == {"value": _VALUE}
        whole = await c.get("t", expect=RESPONSE)
        assert whole.status_code == httpx2.codes.OK


@pytest.mark.parametrize(
    ("verb", "http_method"), [("put", "PUT"), ("patch", "PATCH"), ("post", "POST"), ("delete", "DELETE")]
)
async def test_each_verb_sends_its_own_method_and_decodes(verb: str, http_method: str) -> None:
    """Check that each verb sends its own HTTP method and decodes the reply."""
    wire = _Wire(body={"value": _VALUE})
    async with _client(wire) as c:
        out = await getattr(c, verb)("thing", expect=decoder(_Model))

    assert wire.last.method == http_method
    assert out == _Model(value=_VALUE)


async def test_post_json_body_reaches_content_with_json_content_type() -> None:
    """Check that ``json=`` is serialized compactly by httpx2 and stamped with the JSON content type."""
    wire = _Wire(body={"value": _VALUE})
    async with _client(wire) as c:
        await c.post("thing", json=flavor.to_builtins(_Body(name="x")), expect=decoder(_Model))

    assert b'"name":"x"' in wire.last.content
    assert wire.last.headers.get("content-type") == "application/json"


async def test_head_maps_to_head_method() -> None:
    """Check that ``head`` sends a HEAD."""
    wire = _Wire()
    async with _client(wire) as c:
        out = await c.head("thing", expect=RESPONSE)

    assert wire.last.method == "HEAD"
    assert out.status_code == httpx2.codes.OK


async def test_head_reads_nothing_by_default() -> None:
    """Send a HEAD with no ``expect=`` and return ``None``, as ``expect=nothing`` would."""
    wire = _Wire()
    async with _client(wire) as c:
        out = await c.head("thing")

    assert wire.last.method == "HEAD"
    assert out is None


@pytest.mark.parametrize(
    "status",
    [httpx2.codes.NOT_FOUND, httpx2.codes.INTERNAL_SERVER_ERROR, httpx2.codes.SERVICE_UNAVAILABLE],
)
async def test_head_raises_upstream_status_on_a_returned_non_success(status: int) -> None:
    """Check that HEAD goes through the same pipeline as every verb: an undeclared non-2xx raises."""
    async with _client(_Wire(status)) as c:
        with pytest.raises(UpstreamStatusError) as ei:
            await c.head("thing", expect=nothing)

    assert ei.value.status_code == status


@pytest.mark.parametrize("status", [httpx2.codes.UNAUTHORIZED, httpx2.codes.FORBIDDEN])
async def test_head_maps_an_auth_status_to_auth_error(status: int) -> None:
    """Check that a HEAD's 401/403 is ``AuthError``: a bad credential is this client's configuration."""
    async with _client(_Wire(status)) as c:
        with pytest.raises(AuthError) as ei:
            await c.head("thing", expect=nothing)

    assert ei.value.status_code == status


async def test_head_renders_a_tstring_path_before_naming_it_in_the_error() -> None:
    """Check that the error message names the rendered path, not ``Template(...)``.

    The ``Template`` URL is rendered once, before both the send and any error path. A regression here is invisible to
    every other test: the request still goes to the right URL and only the message is wrong.
    """
    async with _client(_Wire(httpx2.codes.NOT_FOUND)) as c:
        with pytest.raises(UpstreamStatusError) as ei:
            await c.head(t"thing/{'a b'}", expect=nothing)

    assert "thing/a%20b" in str(ei.value)


async def test_an_undeclared_404_raises_even_for_a_bytes_reader() -> None:
    """Check that no reader, not even ``as_bytes``, receives an undeclared non-2xx body as data.

    Otherwise an error page (a CDN's 403, a 404 page) would be returned to the caller as the downloaded file.
    """
    async with _client(_Wire(httpx2.codes.NOT_FOUND, body={"error": "not published"})) as c:
        with pytest.raises(UpstreamStatusError) as ei:
            await c.get("thing", expect=as_bytes)

    assert ei.value.status_code == httpx2.codes.NOT_FOUND


async def test_tstring_path_escapes_interpolation_as_one_segment() -> None:
    """Check that ``BTC/USD``, one symbol, occupies one path segment, not two."""
    wire = _Wire()
    symbol = "BTC/USD"
    async with _client(wire) as c:
        await c.get(t"/v2/positions/{symbol}", expect=nothing)

    assert wire.last.url.raw_path == b"/v2/positions/BTC%2FUSD"


async def test_str_path_reaches_the_wire_verbatim() -> None:
    """Check that a ``str`` path reaches the wire without rendering or escaping."""
    wire = _Wire()
    async with _client(wire) as c:
        await c.get("/v2/positions/BTC/USD", expect=nothing)

    assert wire.last.url.raw_path == b"/v2/positions/BTC/USD"


@pytest.mark.parametrize("verb", ["get", "post", "put", "patch", "delete", "head"])
async def test_every_verb_renders_a_tstring_path(verb: str) -> None:
    """Check that every verb renders and escapes a t-string path."""
    wire = _Wire()
    symbol = "BRK/B"
    async with _client(wire) as c:
        await getattr(c, verb)(t"/v2/positions/{symbol}", expect=nothing)

    assert wire.last.url.raw_path == b"/v2/positions/BRK%2FB"
