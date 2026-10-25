"""The verb methods on ``AsyncBaseClient``: get/post/put/patch/delete/head.

A ``MockTransport`` captures the outgoing ``httpx2.Request``, so each test asserts the verb maps to the right HTTP
method and threads its arguments through. Every verb takes the call's declared responses as ``expect=`` — a bare
:data:`~headgate._responses.Reader` (the success reader) or an :class:`~headgate._responses.Expect` — and an
undeclared non-2xx always raises.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx2
import msgspec
import pytest

from headgate import msgspec as flavor
from headgate._client import AsyncBaseClient
from headgate._rate_limit import Rate
from headgate._responses import as_bytes, nothing
from headgate.errors import AuthError, UpstreamStatusError
from headgate.msgspec import decoder
from tests.support.responses import RESPONSE

if TYPE_CHECKING:
    from collections.abc import Callable


class _Model(msgspec.Struct):
    value: int


def _client(handler: Callable[[httpx2.Request], httpx2.Response]) -> AsyncBaseClient:
    """An AsyncBaseClient whose wire is a MockTransport running `handler`."""
    return AsyncBaseClient(
        base_url="https://example.test",
        rate=Rate.per_second(2),
        transport=httpx2.MockTransport(handler),
        max_attempts=1,
    )


async def test_get_uses_get_method_and_decodes_model() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen["method"] = request.method
        seen["url"] = str(request.url)
        return httpx2.Response(200, json={"value": 7})

    async with _client(handler) as c:
        out = await c.get("thing", expect=decoder(_Model), params={"a": "1"})

    assert seen["method"] == "GET"
    assert seen["url"].endswith("/thing?a=1")
    assert out == _Model(value=7)


async def test_get_decodes_struct_and_json_and_the_whole_response() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json={"value": 3})

    async with _client(handler) as c:
        assert (await c.get("t", expect=decoder(_Model))).value == 3
        assert await c.get("t", expect=decoder(dict[str, Any])) == {"value": 3}
        whole = await c.get("t", expect=RESPONSE)
        assert whole.status_code == 200


# --- post/put/patch/delete/head ---


class _Body(msgspec.Struct):
    name: str


@pytest.mark.parametrize(("verb", "http_method"), [("put", "PUT"), ("patch", "PATCH")])
async def test_body_verb_maps_to_http_method(verb: str, http_method: str) -> None:
    """put/patch take ``expect`` keyword-only (the body itself rides ``json=``/``content=`` in
    ``**kwargs``), so a bodyless decode is the natural keyword form: ``verb(url, expect=...)``."""
    seen: dict[str, Any] = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen["method"] = request.method
        return httpx2.Response(200, json={"value": 5})

    async with _client(handler) as c:
        out = await getattr(c, verb)("thing", expect=decoder(_Model))

    assert seen["method"] == http_method
    assert out == _Model(value=5)


async def test_delete_maps_to_http_method_and_decodes() -> None:
    """delete carries no request body — ``expect`` is keyword-only."""
    seen: dict[str, Any] = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen["method"] = request.method
        return httpx2.Response(200, json={"value": 5})

    async with _client(handler) as c:
        out = await c.delete("thing", expect=decoder(_Model))

    assert seen["method"] == "DELETE"
    assert out == _Model(value=5)


async def test_post_maps_to_post_method_and_decodes_model() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen["method"] = request.method
        return httpx2.Response(200, json={"value": 9})

    async with _client(handler) as c:
        out = await c.post("thing", expect=decoder(_Model))

    assert seen["method"] == "POST"
    assert out == _Model(value=9)


async def test_post_json_body_reaches_content_with_json_content_type() -> None:
    # json=<builtins> rides **kwargs; httpx2 serializes it (compact) and stamps the content-type.
    seen: dict[str, Any] = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen["content"] = request.content
        seen["content_type"] = request.headers.get("content-type")
        return httpx2.Response(200, json={"value": 1})

    async with _client(handler) as c:
        await c.post(
            "thing",
            json=flavor.to_builtins(_Body(name="x")),
            expect=decoder(_Model),
        )

    assert b'"name":"x"' in seen["content"]
    assert seen["content_type"] == "application/json"


async def test_post_raw_json_body_passthrough() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen["content"] = request.content
        return httpx2.Response(200, json={"value": 2})

    async with _client(handler) as c:
        await c.post("thing", expect=decoder(_Model), json={"name": "y"})

    assert b'"name"' in seen["content"]
    assert b'"y"' in seen["content"]


async def test_head_maps_to_head_method() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen["method"] = request.method
        return httpx2.Response(200)

    async with _client(handler) as c:
        out = await c.head("thing", expect=RESPONSE)

    assert seen["method"] == "HEAD"
    assert isinstance(out, httpx2.Response) and out.status_code == 200


@pytest.mark.parametrize("status", [404, 500, 503])
async def test_head_raises_upstream_status_on_a_returned_non_success(status: int) -> None:
    """HEAD goes through the same response pipeline as every verb: an undeclared non-2xx raises."""
    async with _client(lambda request: httpx2.Response(status)) as c:
        with pytest.raises(UpstreamStatusError) as ei:
            await c.head("thing", expect=nothing)

    assert ei.value.status_code == status


@pytest.mark.parametrize("status", [401, 403])
async def test_head_maps_an_auth_status_to_auth_error(status: int) -> None:
    """The 401/403 branch, not merely the non-success one — a bad credential is OUR config."""
    async with _client(lambda request: httpx2.Response(status)) as c:
        with pytest.raises(AuthError) as ei:
            await c.head("thing", expect=nothing)

    assert ei.value.status_code == status


async def test_head_renders_a_tstring_path_before_naming_it_in_the_error() -> None:
    """The error message names the rendered path, not ``Template(...)``.

    ``head`` delegates to ``_request`` like every verb, and the ``Template`` url is rendered once, before both the
    send and any error path. A regression here is invisible to every other test: the request still goes to the
    right URL and only the message is wrong.
    """
    async with _client(lambda request: httpx2.Response(404)) as c:
        with pytest.raises(UpstreamStatusError) as ei:
            await c.head(t"thing/{'a b'}", expect=nothing)

    assert "thing/a%20b" in str(ei.value)


async def test_an_undeclared_404_raises_even_for_a_bytes_reader() -> None:
    """No reader hands back an undeclared non-2xx body as data, not even ``as_bytes``, which accepts any body.

    Otherwise an error page (a CDN's 403, a 404 page) would be returned to the caller as the downloaded file.
    """
    async with _client(lambda request: httpx2.Response(404, content=b"<html>not published</html>")) as c:
        with pytest.raises(UpstreamStatusError) as ei:
            await c.get("thing", expect=as_bytes)

    assert ei.value.status_code == 404


# --- t-string paths ---
# A ``Template`` path is rendered ONCE, while the request is built (``headgate._url.render_path``, which escapes each
# interpolation with ``escape_segment``), so everything after — the send, the error path's ``sanitize_url`` — sees a
# plain ``str``. A ``str`` path is already-trusted structure and reaches the wire verbatim, which is what lets a
# caller forward an absolute cursor URL unescaped.
#
# These assert on ``raw_path``, NOT ``url.path``: httpx2 percent-DECODES the ``path`` property, so
# ``/v2/positions/BTC%2FUSD`` and ``/v2/positions/BTC/USD`` both read back as the latter. Asserting on ``path`` would
# pass whether or not the escaping happened at all. ``raw_path`` is the bytes that go on the wire.


async def test_tstring_path_escapes_interpolation_as_one_segment() -> None:
    """``BTC/USD`` IS the symbol — it must occupy ONE segment, not two."""
    seen: dict[str, Any] = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen["raw_path"] = request.url.raw_path
        return httpx2.Response(200)

    symbol = "BTC/USD"
    async with _client(handler) as c:
        await c.get(t"/v2/positions/{symbol}", expect=nothing)

    assert seen["raw_path"] == b"/v2/positions/BTC%2FUSD"


async def test_str_path_reaches_the_wire_verbatim() -> None:
    """The ``str`` branch is untouched: no rendering, no escaping."""
    seen: dict[str, Any] = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen["raw_path"] = request.url.raw_path
        return httpx2.Response(200)

    async with _client(handler) as c:
        await c.get("/v2/positions/BTC/USD", expect=nothing)

    assert seen["raw_path"] == b"/v2/positions/BTC/USD"


@pytest.mark.parametrize("verb", ["get", "post", "put", "patch", "delete", "head"])
async def test_every_verb_renders_a_tstring_path(verb: str) -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen["raw_path"] = request.url.raw_path
        return httpx2.Response(200)

    symbol = "BRK/B"
    async with _client(handler) as c:
        await getattr(c, verb)(t"/v2/positions/{symbol}", expect=nothing)

    assert seen["raw_path"] == b"/v2/positions/BRK%2FB"
