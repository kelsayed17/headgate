# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""``AsyncCurlTransport``: a curl response comes back as the httpx2 response a native transport would give.

Most tests replace ``curl_cffi``'s ``AsyncSession`` with a recording double; the ``TestRealCurl`` tests drive real curl
against a local HTTP server, for the behaviour only libcurl itself can show (decompression, aborting a transfer).
"""

from __future__ import annotations

import asyncio
import gzip
import importlib
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Any, ClassVar

import anysqlite
import httpx2
import pytest
from curl_cffi import CurlECode, CurlHttpVersion, CurlOpt
from curl_cffi.requests.exceptions import RequestException
from hishel import AsyncSqliteStorage
from hishel.httpx2 import AsyncCacheTransport

import headgate.curl
from headgate import _curl
from headgate.curl import AsyncCurlTransport

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Iterator

_URL = "https://example.test/"
_CREATED = 201
_CONNECT_TIMEOUT = 2.0
_READ_TIMEOUT = 7.0
_PLAIN = b"already plain"
_CLOSE_DEADLINE = 5.0


class _Headers:
    """The ``raw`` view of ``curl_cffi``'s headers, the only part the transport reads."""

    def __init__(self, raw: list[tuple[bytes, bytes | None]]) -> None:
        self.raw = raw


class _CurlResponse:
    """The slice of a streamed ``curl_cffi.requests.Response`` the transport reads."""

    def __init__(
        self,
        *,
        status: int = 200,
        headers: list[tuple[bytes, bytes | None]] | None = None,
        chunks: list[bytes] | None = None,
        failure: RequestException | None = None,
    ) -> None:
        self.status_code = status
        self.headers = _Headers(headers or [])
        self.reason = "OK"
        self.infos: dict[str, Any] = {}
        self.http_version: int = CurlHttpVersion.V2_0
        self.quit_now = asyncio.Event()
        self._chunks = chunks or []
        self._failure = failure
        self.closed = False

    async def aiter_content(self) -> AsyncIterator[bytes]:
        """Yield the body's chunks, then raise the configured mid-body failure, if any."""
        for chunk in self._chunks:
            yield chunk
        if self._failure is not None:
            raise self._failure

    async def aclose(self) -> None:
        """Record the close."""
        self.closed = True


class _Session:
    """Stand in for ``curl_cffi``'s ``AsyncSession``, recording what it was built and called with."""

    def __init__(self, **kwargs: object) -> None:
        self.init: dict[str, Any] = kwargs
        self.calls: list[dict[str, Any]] = []
        self.respond: Callable[[], _CurlResponse] = _CurlResponse
        self.failure: RequestException | None = None
        self.closed = False

    async def request(self, **kwargs: object) -> _CurlResponse:
        """Record the call and return a fresh response, or raise the configured failure."""
        self.calls.append(kwargs)
        if self.failure is not None:
            raise self.failure
        return self.respond()

    async def close(self) -> None:
        """Record the close."""
        self.closed = True


@pytest.fixture
def sessions(monkeypatch: pytest.MonkeyPatch) -> list[_Session]:
    """Replace ``AsyncSession`` with :class:`_Session` and return every session built."""
    made: list[_Session] = []

    def build(**kwargs: object) -> _Session:
        made.append(_Session(**kwargs))
        return made[-1]

    monkeypatch.setattr(_curl, "_AsyncSession", build)
    return made


def _transport(
    sessions: list[_Session], *, default_headers: bool = False, curl_options: dict[CurlOpt, Any] | None = None
) -> tuple[AsyncCurlTransport, _Session]:
    """Build a transport over a recording session and return both."""
    transport = AsyncCurlTransport(impersonate="chrome", default_headers=default_headers, curl_options=curl_options)
    return transport, sessions[-1]


async def _get(transport: httpx2.AsyncBaseTransport, limits: httpx2.Timeout | None = None) -> httpx2.Response:
    """Send one GET through *transport* (with httpx2's default timeouts unless *limits*) and return the response."""
    async with httpx2.AsyncClient(transport=transport, timeout=limits or httpx2.Timeout(5.0)) as client:
        return await client.get(_URL)


def test_the_public_module_reexports_the_transport() -> None:
    """Expose the transport as ``headgate.curl.AsyncCurlTransport``."""
    assert headgate.curl.AsyncCurlTransport is _curl.AsyncCurlTransport
    assert headgate.curl.__all__ == ["AsyncCurlTransport"]


def test_import_without_curl_cffi_names_the_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail the import with a message naming ``headgate[curl]`` when ``curl_cffi`` is missing."""
    monkeypatch.setitem(sys.modules, "curl_cffi", None)
    monkeypatch.delitem(sys.modules, "headgate._curl")
    with pytest.raises(ImportError, match=r"headgate\[curl\]"):
        importlib.import_module("headgate._curl")


def test_the_session_ignores_the_ambient_proxy_keeps_no_cookie_and_follows_no_redirect(
    sessions: list[_Session],
) -> None:
    """Build the curl session so the stack above sees every hop, status and cookie itself."""
    _, session = _transport(sessions, curl_options={CurlOpt.FRESH_CONNECT: True})
    assert session.init["curl_options"] == {CurlOpt.FRESH_CONNECT: True, CurlOpt.NOPROXY: "*"}
    assert session.init["allow_redirects"] is False
    assert session.init["trust_env"] is False
    assert session.init["discard_cookies"] is True
    assert session.init["impersonate"] == "chrome"


async def test_a_request_goes_through_curl_and_comes_back_as_an_httpx_response(sessions: list[_Session]) -> None:
    """Send method, URL, body, headers and timeouts to curl, and map status, headers, body and version back."""
    transport, session = _transport(sessions)
    body = b'{"ok": true}'
    session.respond = lambda: _CurlResponse(
        status=_CREATED,
        headers=[(b"Content-Type", b"application/json"), (b"X-Empty", None)],
        chunks=[body],
    )
    timeout = httpx2.Timeout(_READ_TIMEOUT, connect=_CONNECT_TIMEOUT)
    async with httpx2.AsyncClient(transport=transport, timeout=timeout) as client:
        response = await client.post(f"{_URL}path?q=1", content=b"payload", headers={"X-Test": "1"})
    assert (response.status_code, response.content) == (_CREATED, body)
    assert response.headers["content-type"] == "application/json"
    assert response.headers["x-empty"] == ""
    assert response.http_version == "HTTP/2"
    assert response.reason_phrase == "OK"
    (call,) = session.calls
    assert call["method"] == "POST"
    assert call["url"] == f"{_URL}path?q=1"
    assert call["data"] == b"payload"
    assert call["stream"] is True
    assert call["timeout"] == (_CONNECT_TIMEOUT, _READ_TIMEOUT)
    assert (b"x-test", b"1") in [(k.lower(), v) for k, v in call["headers"]]


async def test_an_unknown_http_version_and_empty_reason_fall_back_to_httpxs_defaults(
    sessions: list[_Session],
) -> None:
    """Leave out the version and reason extensions when curl reports none, so httpx2's defaults apply."""
    transport, session = _transport(sessions)

    def respond() -> _CurlResponse:
        curl = _CurlResponse()
        curl.reason = ""
        curl.http_version = CurlHttpVersion.NONE
        return curl

    session.respond = respond
    response = await _get(transport)
    assert "http_version" not in response.extensions
    assert "reason_phrase" not in response.extensions
    assert response.reason_phrase == "OK"


@pytest.mark.parametrize(
    ("connect", "read", "expected"),
    [(None, None, None), (None, _READ_TIMEOUT, (0.0, _READ_TIMEOUT)), (_CONNECT_TIMEOUT, None, None)],
)
async def test_an_unlimited_timeout_becomes_curls_no_limit(
    sessions: list[_Session], connect: float | None, read: float | None, expected: tuple[float, float] | None
) -> None:
    """Map a ``None`` timeout onto curl's own "no limit" instead of failing on ``None + float``."""
    transport, session = _transport(sessions)
    await _get(transport, limits=httpx2.Timeout(None, connect=connect, read=read))
    (call,) = session.calls
    assert call["timeout"] == expected


async def test_an_encoded_body_is_not_decoded_a_second_time(sessions: list[_Session]) -> None:
    """Drop ``Content-Encoding`` and ``Content-Length``: curl already decoded the body they describe."""
    transport, session = _transport(sessions)
    session.respond = lambda: _CurlResponse(
        headers=[(b"Content-Encoding", b"gzip"), (b"Content-Length", b"3"), (b"X-Kept", b"1")], chunks=[_PLAIN]
    )
    response = await _get(transport)
    assert response.content == _PLAIN
    assert "content-encoding" not in response.headers
    assert "content-length" not in response.headers
    assert response.headers["x-kept"] == "1"
    received = response.extensions["curl"]["response"].headers.raw
    assert (b"Content-Encoding", b"gzip") in received


async def test_an_unencoded_body_keeps_its_content_length(sessions: list[_Session]) -> None:
    """Keep ``Content-Length`` when the body was not encoded, since it then describes the bytes delivered."""
    transport, session = _transport(sessions)
    session.respond = lambda: _CurlResponse(headers=[(b"Content-Length", b"13")], chunks=[_PLAIN])
    response = await _get(transport)
    assert response.headers["content-length"] == str(len(_PLAIN))


async def test_a_cached_encoded_response_replays_without_a_decoding_error(sessions: list[_Session]) -> None:
    """Replay a cached gzip response as plain bytes: hishel stores the raw stream with the headers beside it."""
    transport, session = _transport(sessions)
    session.respond = lambda: _CurlResponse(
        headers=[(b"Content-Encoding", b"gzip"), (b"Cache-Control", b"max-age=60")], chunks=[_PLAIN]
    )
    connection = await anysqlite.connect(":memory:")
    storage = AsyncSqliteStorage(connection=connection)
    cache = AsyncCacheTransport(next_transport=transport, storage=storage)
    try:
        async with httpx2.AsyncClient(transport=cache) as client:
            first = await client.get(_URL)
            second = await client.get(_URL)
    finally:
        await storage.close()
        await connection.close()
    assert second.extensions["hishel_from_cache"] is True
    assert first.content == second.content == _PLAIN
    assert len(session.calls) == 1


async def test_a_streamed_body_arrives_chunk_by_chunk_and_is_closed(sessions: list[_Session]) -> None:
    """Yield each curl chunk in order, then close the curl response."""
    transport, session = _transport(sessions)
    curl = _CurlResponse(chunks=[b"ab", b"cd"])
    session.respond = lambda: curl
    async with httpx2.AsyncClient(transport=transport) as client, client.stream("GET", _URL) as response:
        assert [chunk async for chunk in response.aiter_raw()] == [b"ab", b"cd"]
    assert curl.closed


async def test_closing_a_response_early_tells_curl_to_abort(sessions: list[_Session]) -> None:
    """Set ``quit_now`` on close, so curl aborts the transfer instead of queueing the rest of the body."""
    transport, session = _transport(sessions)
    curl = _CurlResponse(chunks=[b"ab", b"cd"])
    session.respond = lambda: curl
    async with httpx2.AsyncClient(transport=transport) as client, client.stream("GET", _URL):
        pass
    assert curl.quit_now.is_set()
    assert curl.closed


@pytest.mark.parametrize(
    ("code", "message", "expected"),
    [
        (CurlECode.RECV_ERROR, "Recv failure: Connection reset by peer", httpx2.ReadError),
        (CurlECode.PARTIAL_FILE, "transfer closed with outstanding read data", httpx2.RemoteProtocolError),
        (CurlECode.OPERATION_TIMEDOUT, "Operation too slow.", httpx2.ReadTimeout),
    ],
)
async def test_a_curl_error_mid_body_becomes_an_httpx_error(
    sessions: list[_Session], code: CurlECode, message: str, expected: type[httpx2.RequestError]
) -> None:
    """Raise a failure while streaming the body as the httpx2 error a native transport raises there."""
    transport, session = _transport(sessions)
    session.respond = lambda: _CurlResponse(chunks=[b"ab"], failure=RequestException(message, code))
    with pytest.raises(expected, match=message):
        await _get(transport)


async def test_default_headers_drop_httpxs_own_defaults_but_keep_one_the_caller_set(sessions: list[_Session]) -> None:
    """Leave the browser headers to curl's profile, except a header the caller set themselves."""
    transport, session = _transport(sessions, default_headers=True)
    async with httpx2.AsyncClient(transport=transport) as client:
        await client.get(_URL, headers={"Accept": "application/vnd.test+json"})
    (call,) = session.calls
    sent = {k.lower(): v for k, v in call["headers"]}
    assert b"user-agent" not in sent
    assert sent[b"accept"] == b"application/vnd.test+json"


async def test_without_default_headers_httpxs_defaults_are_sent(sessions: list[_Session]) -> None:
    """Send httpx2's own default headers when the profile is not asked to supply any."""
    transport, session = _transport(sessions, default_headers=False)
    await _get(transport)
    (call,) = session.calls
    assert b"user-agent" in {k.lower() for k, _ in call["headers"]}


@pytest.mark.parametrize(
    ("code", "message", "expected"),
    [
        (CurlECode.COULDNT_CONNECT, "Failed to connect", httpx2.ConnectError),
        (CurlECode.COULDNT_RESOLVE_HOST, "Could not resolve host", httpx2.ConnectError),
        (CurlECode.SSL_CONNECT_ERROR, "TLS connect error", httpx2.ConnectError),
        (CurlECode.UNSUPPORTED_PROTOCOL, "Protocol not supported", httpx2.UnsupportedProtocol),
        (CurlECode.PROXY, "proxy handshake", httpx2.ProxyError),
        (CurlECode.SEND_ERROR, "Send failure", httpx2.WriteError),
        (CurlECode.GOT_NOTHING, "Empty reply from server", httpx2.RemoteProtocolError),
        (CurlECode.BAD_CONTENT_ENCODING, "Unrecognized content encoding", httpx2.DecodingError),
        (CurlECode.OPERATION_TIMEDOUT, "Connection timed out after 1000 milliseconds", httpx2.ConnectTimeout),
        (CurlECode.OPERATION_TIMEDOUT, "Resolving timed out after 1000 milliseconds", httpx2.ConnectTimeout),
        (CurlECode.OPERATION_TIMEDOUT, "Operation too slow. Less than 1 bytes/sec", httpx2.ReadTimeout),
        (CurlECode.GOT_NOTHING.value + 1000, "a code curl_cffi does not know", httpx2.TransportError),
        (0, "no code at all", httpx2.TransportError),
    ],
)
async def test_a_curl_error_becomes_the_httpx_error_a_native_transport_raises(
    sessions: list[_Session], code: CurlECode, message: str, expected: type[httpx2.RequestError]
) -> None:
    """Map a failure to send onto the httpx2 exception class the policy layers classify, keeping curl's message."""
    transport, session = _transport(sessions)
    session.failure = RequestException(message, code)
    with pytest.raises(expected, match=message) as raised:
        await _get(transport)
    assert raised.type is expected


async def test_a_method_curl_cannot_send_is_refused_before_any_request(sessions: list[_Session]) -> None:
    """Refuse a method outside curl_cffi's set without touching the session."""
    transport, session = _transport(sessions)
    async with httpx2.AsyncClient(transport=transport) as client:
        with pytest.raises(httpx2.UnsupportedProtocol):
            await client.request("PROPFIND", _URL)
    assert session.calls == []


async def test_closing_the_transport_closes_the_curl_session(sessions: list[_Session]) -> None:
    """Close the curl session with the transport."""
    transport, session = _transport(sessions)
    await transport.aclose()
    assert session.closed


class _Handler(BaseHTTPRequestHandler):
    """Serve ``/gzip`` as a gzip-encoded body and ``/endless`` as a body that never ends."""

    chunk: ClassVar[bytes] = b"x" * 65536

    def do_GET(self) -> None:
        """Answer the two paths."""
        self.send_response(200)
        if self.path == "/gzip":
            body = gzip.compress(_PLAIN)
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.end_headers()
        try:
            while True:
                self.wfile.write(self.chunk)
        except OSError:  # the client aborted, which is what the test wants
            return


@pytest.fixture
def server() -> Iterator[str]:
    """Run :class:`_Handler` on a loopback port and return its base URL."""
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}"
    finally:
        httpd.shutdown()
        httpd.server_close()


class TestRealCurl:
    """Drive real curl against a local server."""

    async def test_curl_decodes_the_body_and_the_response_says_so(self, server: str) -> None:
        """Deliver the decoded body once, without the encoding headers that no longer describe it."""
        transport = AsyncCurlTransport(impersonate="chrome")
        async with httpx2.AsyncClient(transport=transport) as client:
            response = await client.get(f"{server}/gzip")
        assert response.content == _PLAIN
        assert "content-encoding" not in response.headers

    async def test_closing_a_response_early_aborts_an_endless_transfer(self, server: str) -> None:
        """Return from close promptly on a body that never ends, instead of waiting for all of it."""
        transport = AsyncCurlTransport(impersonate="chrome")
        async with httpx2.AsyncClient(transport=transport) as client, client.stream("GET", f"{server}/endless") as r:
            async with asyncio.timeout(_CLOSE_DEADLINE):
                async for _ in r.aiter_raw():
                    break
                await r.aclose()
        assert r.is_closed
