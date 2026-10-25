# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""What a ``ClientError`` says, and what it never says.

Every message can reach an application boundary's error detail, so each names the request line and the status in the
same shape, is bounded, and never repeats a credential: not a header value the HTTP library quotes back, not a URL
pasted whole, not a body. A reader's or a ``refusal_check``'s failure, however it is raised, lands in the same tree.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import TYPE_CHECKING

import httpx2
import pytest

from headgate import Expect, HttpOptions, RefusedError, StaticHeaderAuth, TransportPolicy
from headgate._breaker import HOST_BREAKERS
from headgate._failures import MESSAGE_TEXT_LIMIT, transport_error
from headgate._rate_limit import UNLIMITED
from headgate._responses import as_bytes
from headgate.errors import (
    AuthError,
    ClientError,
    RateLimitError,
    ResponseParseError,
    TransportError,
    UpstreamStatusError,
)
from tests.support.clients import StubClient

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from headgate._responses import RefusalCheck

_HOST = "example.test"
#: The credential every test that could leak one carries.
_CANARY = "sk-live-SECRET123"
#: A cap on a URL named in a message, with room to spare for the rest of the message.
_URL_IN_MESSAGE = 400


def _client(
    handler: Callable[[httpx2.Request], httpx2.Response], *, refusal_check: RefusalCheck | None = None
) -> StubClient:
    """Return a client over *handler* that sends each request once."""
    return StubClient(
        base_url=f"https://{_HOST}",
        rate=UNLIMITED,
        policy=TransportPolicy(max_attempts=1),
        http=HttpOptions(
            transport=httpx2.MockTransport(handler),
            refusal_check=refusal_check,
            follow_redirects=refusal_check is None,
        ),
    )


def _answering(status: int, content: bytes = b"") -> Callable[[httpx2.Request], httpx2.Response]:
    """Return a handler answering every request with *status* and *content*."""
    return lambda _request: httpx2.Response(status, content=content)


# --- A request the HTTP library refuses to encode never quotes its header values ---


@pytest.fixture
async def local_server() -> AsyncIterator[str]:
    """Serve ``200 ok`` on a loopback port and yield its base URL, so a real HTTP/1.1 connection encodes requests."""

    async def answer(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        # A client that refuses to encode its request connects and hangs up without sending a byte.
        with contextlib.suppress(asyncio.IncompleteReadError):
            await reader.readuntil(b"\r\n\r\n")
            writer.write(b"HTTP/1.1 200 OK\r\ncontent-length: 2\r\n\r\nok")
            await writer.drain()
        writer.close()

    server = await asyncio.start_server(answer, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.close()
        await server.wait_closed()


async def test_a_header_value_the_http_library_refuses_is_never_quoted(local_server: str) -> None:
    """A token with a trailing newline makes h11 refuse the header, quoting the whole value; the error names the header.

    The message is fixed and value-free, and nothing is chained, since the cause carries the same text.
    """
    client = StubClient(
        base_url=local_server,
        rate=UNLIMITED,
        policy=TransportPolicy(max_attempts=1),
        auth=StaticHeaderAuth({"Authorization": f"Bearer {_CANARY}\n"}),
    )
    try:
        with pytest.raises(TransportError) as raised:
            await client.get("x", expect=as_bytes)
    finally:
        await client.aclose()

    assert _CANARY not in str(raised.value)
    assert raised.value.__cause__ is None
    assert raised.value.__suppress_context__
    assert "LocalProtocolError" in str(raised.value)
    assert "illegal value in header Authorization" in str(raised.value)


def test_an_unencodable_request_without_a_request_attached_gets_the_fixed_message() -> None:
    """Fall back to the fixed message when the error names no request, as a transport double's may not."""
    error = transport_error(httpx2.LocalProtocolError(f"Illegal header value {_CANARY!r}"), "GET", "x", source="S")

    assert str(error) == "[S] GET x: the request could not be encoded (LocalProtocolError)"


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        pytest.param([(b"X-Ok", b"fine")], "(LocalProtocolError)", id="no-header-is-at-fault"),
        pytest.param([(b"X Bad", b"a\r\nb")], "illegal value in header <invalid name>", id="an-illegal-name-too"),
    ],
)
def test_an_unencodable_request_names_only_legal_header_names(
    headers: list[tuple[bytes, bytes]], expected: str
) -> None:
    """Name a header only when its own name is a legal token, and none when every value is legal."""
    request = httpx2.Request("GET", "https://example.test/x", headers=headers)
    exc = httpx2.LocalProtocolError("Illegal header", request=request)

    assert str(transport_error(exc, "GET", "x", source="S")).endswith(expected)


# --- A surviving 429/5xx reads like every other status error ---


@pytest.mark.parametrize(
    ("status", "kind"),
    [(httpx2.codes.SERVICE_UNAVAILABLE, UpstreamStatusError), (httpx2.codes.TOO_MANY_REQUESTS, RateLimitError)],
)
async def test_a_surviving_transient_status_names_the_method_the_relative_url_and_the_body(
    status: int, kind: type[UpstreamStatusError]
) -> None:
    """Name the request line as the call wrote it, with the credential redacted, and the provider's reason."""
    async with _client(_answering(status, b'{"error": "maintenance until 5pm"}')) as client:
        with pytest.raises(kind) as raised:
            await client.get("series?api_key=SECRET", expect=as_bytes)

    assert str(raised.value) == (
        f'[StubClient] transient {status} for GET series?api_key=%2A%2A%2A (status={status}) — {{"error": '
        '"maintenance until 5pm"}'
    )


async def test_an_undeclared_status_names_the_method() -> None:
    """Name the method in the status ladder's error too, so every status error reads alike."""
    async with _client(_answering(httpx2.codes.NOT_FOUND)) as client:
        with pytest.raises(UpstreamStatusError, match=r"http error for DELETE orders/1 \(status=404\)"):
            await client.delete("orders/1", expect=as_bytes)


# --- A URL the client cannot use ---


async def test_an_unclosed_ipv6_bracket_is_a_client_error_with_no_credential() -> None:
    """Raise the ``ClientError`` every bad URL raises, rather than ``urlsplit``'s raw ``ValueError``."""
    async with _client(_answering(httpx2.codes.OK)) as client:
        with pytest.raises(ClientError, match="invalid URL") as raised:
            await client.get("http://[::1/x?api_key=SECRET", expect=as_bytes)

    assert type(raised.value) is ClientError
    assert "SECRET" not in str(raised.value)


async def test_a_very_long_bad_url_is_named_in_a_bounded_message() -> None:
    """Cut a URL to a few hundred characters in the message, so a 65,536-character URL is not a 65 KB log line."""
    async with _client(_answering(httpx2.codes.OK)) as client:
        with pytest.raises(ClientError, match="invalid URL") as raised:
            await client.get("x" * 70_000, expect=as_bytes)

    assert len(str(raised.value)) < _URL_IN_MESSAGE


# --- A reader's failure lands in the tree ---


def _json_key(resp: httpx2.Response) -> object:
    """Read the ``"value"`` field of the body, as hand-written reader code does."""
    return resp.json()["value"]


def _first_item(resp: httpx2.Response) -> object:
    """Read the first item of a list body."""
    return resp.json()[0]


@pytest.mark.parametrize(
    ("body", "raised"),
    [
        pytest.param(b"not json", json.JSONDecodeError, id="json-decode-error"),
        pytest.param(b'{"other": 1}', KeyError, id="key-error"),
        pytest.param(b"[]", IndexError, id="index-error"),
        pytest.param(b'"text"', TypeError, id="type-error"),
    ],
)
async def test_a_hand_written_reader_failing_on_a_2xx_is_a_parse_error(body: bytes, raised: type[Exception]) -> None:
    """Map what reader code over ``response.json()`` raises on a body of the wrong shape to ``ResponseParseError``."""
    reader = _first_item if raised is IndexError else _json_key
    async with _client(_answering(httpx2.codes.OK, body)) as client:
        with pytest.raises(ResponseParseError) as caught:
            await client.get("series", expect=reader)

    assert isinstance(caught.value.__cause__, raised)


async def test_a_parse_error_names_the_status_and_the_request_line() -> None:
    """Name the status and the method with the URL, so the failing call can be found from the message alone."""
    async with _client(_answering(httpx2.codes.CREATED, b"not json")) as client:
        with pytest.raises(ResponseParseError, match=r"unreadable 201 body for POST orders: Expecting value"):
            await client.post("orders", expect=_json_key)


async def test_a_parse_error_message_is_bounded() -> None:
    """Bound a reader's diagnosis, which can quote as much of the body as it likes."""

    def verbose(resp: httpx2.Response) -> object:
        raise ValueError(resp.text)

    async with _client(_answering(httpx2.codes.OK, b"x" * (10 * MESSAGE_TEXT_LIMIT))) as client:
        with pytest.raises(ResponseParseError) as caught:
            await client.get("series", expect=verbose)

    assert len(str(caught.value)) < 2 * MESSAGE_TEXT_LIMIT


async def test_a_reader_failing_on_a_declared_non_2xx_falls_through_to_the_status_ladder() -> None:
    """Treat a declared 404 whose body is not the declared shape as the 404 it is, as a ``DecodeError`` would be."""
    async with _client(_answering(httpx2.codes.NOT_FOUND, b'{"other": 1}')) as client:
        with pytest.raises(UpstreamStatusError) as caught:
            await client.get("series", expect=Expect(as_bytes).on(httpx2.codes.NOT_FOUND, _json_key))

    assert caught.value.status_code == httpx2.codes.NOT_FOUND
    assert isinstance(caught.value.__cause__, KeyError)


async def test_a_readers_refusal_is_chained_to_the_error_it_becomes() -> None:
    """Chain the ``ClientError`` to the reader's ``RefusedError``, so the traceback shows where the reader refused."""
    refusal = RefusedError("quota exhausted", code="Q", kind=RateLimitError)

    def refuse(_resp: httpx2.Response) -> bytes:
        raise refusal

    async with _client(_answering(httpx2.codes.OK)) as client:
        with pytest.raises(RateLimitError) as caught:
            await client.get("series", expect=refuse)

    assert caught.value.__cause__ is refusal


# --- A refusal_check that raises rather than returns ---


def _raising_check(resp: httpx2.Response) -> RefusedError | None:
    """Refuse a redirect by raising, as a check written like a reader might."""
    if resp.has_redirect_location:
        msg = "bad key"
        raise RefusedError(msg, code="KEY", kind=AuthError)
    return None


def _redirecting(_request: httpx2.Request) -> httpx2.Response:
    """Answer with a redirect to a sign-in page."""
    return httpx2.Response(httpx2.codes.FOUND, headers={"location": "/login"})


async def test_a_refusal_check_that_raises_is_treated_like_one_that_returns() -> None:
    """Map a raised ``RefusedError`` from the host check to its ``ClientError``, with the status the wire sent."""
    async with _client(_redirecting, refusal_check=_raising_check) as client:
        with pytest.raises(AuthError) as caught:
            await client.get("series", expect=as_bytes)

    assert caught.value.status_code == httpx2.codes.FOUND
    assert caught.value.code == "KEY"
    assert HOST_BREAKERS[_HOST].failure_count == 0


async def test_a_refusal_check_that_raises_on_a_stream_is_treated_like_one_that_returns() -> None:
    """Map a raised ``RefusedError`` the same way on ``stream_get``, before any chunk."""
    async with _client(_redirecting, refusal_check=_raising_check) as client:
        with pytest.raises(AuthError, match="bad key"):
            async for _ in client.stream_get("big.csv"):
                pass
