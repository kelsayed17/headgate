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

from typing import assert_type

import httpx2
import msgspec
import pytest

from headgate import EncodedBody, Expect, HttpOptions, TransportPolicy, Writer
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


class _Order(msgspec.Struct, frozen=True, omit_defaults=True):
    """A request body with a renamed field and an optional one."""

    sku: str = msgspec.field(name="SKU")
    note: str | None = None


_NEW_ORDER = flavor.encoder(_Order)


async def test_body_sends_what_an_encoder_wrote_with_its_content_type() -> None:
    """Send an encoder's bytes, renames and ``omit_defaults`` applied, as ``application/json``."""
    assert_type(_NEW_ORDER, Writer[_Order])
    wire = _Wire(body={"value": _VALUE})
    async with _client(wire) as c:
        await c.post("orders", body=_NEW_ORDER(_Order(sku="A-1")), expect=decoder(_Model))

    assert wire.last.content == b'{"SKU":"A-1"}'
    assert wire.last.headers["content-type"] == "application/json"


async def test_a_calls_content_type_wins_over_the_writers() -> None:
    """Keep the ``Content-Type`` a call's ``headers=`` set, as for any header the call sets."""
    wire = _Wire(body={"value": _VALUE})
    async with _client(wire) as c:
        body = _NEW_ORDER(_Order(sku="A-1"))
        await c.put("orders/1", body=body, headers={"Content-Type": "application/merge-patch+json"}, expect=nothing)

    assert wire.last.headers["content-type"] == "application/merge-patch+json"


def _defaulting(wire: _Wire, headers: dict[str, str]) -> AsyncBaseClient:
    """Return a client over *wire* sending *headers* on every call, as client-wide defaults."""
    return AsyncBaseClient(
        base_url="https://example.test", http=HttpOptions(transport=httpx2.MockTransport(wire), headers=headers)
    )


async def test_the_writers_content_type_wins_over_the_clients_default() -> None:
    """Send the encoded body's ``Content-Type`` over one among the client's default headers."""
    wire = _Wire()
    async with _defaulting(wire, {"Content-Type": "application/json"}) as c:
        await c.post("x", body=EncodedBody(b"\x81", "application/msgpack"), expect=nothing)
        await c.with_options(headers={"Content-Type": "application/cbor"}).post(
            "x", body=EncodedBody(b"\x81", "application/msgpack"), expect=nothing
        )

    first, from_a_view = wire.sent
    assert first.headers.get_list("content-type") == ["application/msgpack"]
    assert from_a_view.headers.get_list("content-type") == ["application/cbor"]


async def test_a_declarations_headers_win_over_the_clients_defaults_but_not_the_calls() -> None:
    """Send ``Expect.headers`` over the client's default headers, and keep one the call set itself."""
    wire = _Wire()
    spec = Expect(nothing, headers={"If-None-Match": '"v2"', "Prefer": "return=minimal"})
    async with _defaulting(wire, {"If-None-Match": '"v1"', "Prefer": "return=representation"}) as c:
        await c.get("x", expect=spec, headers={"prefer": "respond-async"})

    assert wire.last.headers.get_list("if-none-match") == ['"v2"']
    assert wire.last.headers.get_list("prefer") == ["respond-async"]


async def test_an_encoded_body_is_sent_again_on_a_retry() -> None:
    """Re-send the whole encoded body on a retry, since it is bytes."""
    statuses = iter((503, 200))
    sent: list[bytes] = []

    def answer(request: httpx2.Request) -> httpx2.Response:
        sent.append(request.content)
        return httpx2.Response(next(statuses))

    client = AsyncBaseClient(base_url="https://example.test", http=HttpOptions(transport=httpx2.MockTransport(answer)))
    async with client:
        await client.put("orders/1", body=_NEW_ORDER(_Order(sku="A-1", note=None)), expect=nothing)

    assert sent == [b'{"SKU":"A-1"}'] * 2


def test_drop_none_leaves_out_none_fields_a_struct_would_send() -> None:
    """Leave out a ``None`` field that ``omit_defaults`` keeps, because its default is not ``None``."""

    class _Patch(msgspec.Struct, frozen=True):
        """A partial update whose unset fields must not be sent as ``null``."""

        name: str | None = "unchanged"
        size: int | None = None

    assert flavor.encoder(_Patch, drop_none=True)(_Patch(name=None, size=3)).content == b'{"size":3}'
    assert flavor.encoder(_Patch)(_Patch(name=None, size=3)).content == b'{"name":null,"size":3}'


async def test_body_combines_with_no_other_body_option() -> None:
    """Refuse ``body=`` beside ``json=``, which httpx2 would drop one of."""
    async with _client(_Wire()) as c:
        with pytest.raises(ValueError, match="body= is the whole request body"):
            await c.post("orders", body=_NEW_ORDER(_Order(sku="A-1")), json={}, expect=nothing)


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
