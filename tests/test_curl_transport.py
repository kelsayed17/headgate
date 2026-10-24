"""`AsyncCurlTransport`: a curl response comes back as the httpx2 response a native transport would give."""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import httpx2
import pytest
from curl_cffi import CurlECode, CurlHttpVersion, CurlOpt
from curl_cffi.requests.exceptions import RequestException

from headgate import _curl_transport
from headgate._curl_transport import AsyncCurlTransport

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


class _Headers:
    def __init__(self, raw: list[tuple[bytes, bytes | None]]) -> None:
        self.raw = raw


class _CurlResponse:
    """The slice of ``curl_cffi.requests.Response`` the transport reads."""

    def __init__(
        self,
        *,
        status: int = 200,
        body: bytes = b"",
        headers: list[tuple[bytes, bytes | None]] | None = None,
        reason: str = "OK",
        chunks: list[bytes] | None = None,
    ) -> None:
        self.status_code = status
        self.content = body
        self.headers = _Headers(headers or [])
        self.reason = reason
        self.request = SimpleNamespace(headers=None)
        self.infos: dict[str, Any] = {}
        self.http_version = int(CurlHttpVersion.V2_0)
        self.elapsed = timedelta(milliseconds=12)
        self.queue = object() if chunks is not None else None
        self._chunks = chunks or []
        self.closed = False

    async def aiter_content(self) -> AsyncIterator[bytes]:
        for chunk in self._chunks:
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


class _Session:
    """Stands in for ``curl_cffi``'s ``AsyncSession``: records what it was built and called with."""

    def __init__(self, **kwargs: object) -> None:
        self.init: dict[str, Any] = kwargs
        self.calls: list[dict[str, Any]] = []
        self.result: _CurlResponse | Exception = _CurlResponse()
        self.closed = False

    async def request(self, **kwargs: object) -> _CurlResponse:
        self.calls.append(kwargs)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    async def close(self) -> None:
        self.closed = True


@pytest.fixture
def sessions(monkeypatch: pytest.MonkeyPatch) -> list[_Session]:
    made: list[_Session] = []

    def build(**kwargs: object) -> _Session:
        made.append(_Session(**kwargs))
        return made[-1]

    monkeypatch.setattr(_curl_transport, "_AsyncSession", build)
    return made


def _transport(
    sessions: list[_Session], *, default_headers: bool = False, curl_options: dict[CurlOpt, Any] | None = None
) -> tuple[AsyncCurlTransport, _Session]:
    transport = AsyncCurlTransport(impersonate="chrome", default_headers=default_headers, curl_options=curl_options)
    return transport, sessions[-1]


def test_the_session_ignores_the_ambient_proxy_keeps_no_cookie_and_follows_no_redirect(
    sessions: list[_Session],
) -> None:
    _, session = _transport(sessions, curl_options={CurlOpt.FRESH_CONNECT: True})
    assert session.init["curl_options"] == {CurlOpt.FRESH_CONNECT: True, CurlOpt.NOPROXY: "*"}
    assert session.init["allow_redirects"] is False
    assert session.init["trust_env"] is False
    assert session.init["impersonate"] == "chrome"
    jar = session.init["cookies"]
    jar.set_cookie(object())  # type: ignore[arg-type]  # discarded, so nothing to validate
    assert len(jar) == 0


async def test_a_request_goes_through_curl_and_comes_back_as_an_httpx_response(sessions: list[_Session]) -> None:
    transport, session = _transport(sessions, default_headers=True)
    session.result = _CurlResponse(
        status=201,
        body=b'{"ok": true}',
        headers=[(b"Content-Type", b"application/json"), (b"X-Empty", None)],
    )
    async with httpx2.AsyncClient(transport=transport) as client:
        response = await client.post("https://example.test/path?q=1", content=b"payload", headers={"X-Test": "1"})
    assert (response.status_code, response.content) == (201, b'{"ok": true}')
    assert response.headers["content-type"] == "application/json"
    assert response.headers["x-empty"] == ""
    assert response.extensions["http_version"] == b"HTTP/2"
    assert response.elapsed == timedelta(milliseconds=12)
    (call,) = session.calls
    assert call["method"] == "POST"
    assert call["url"] == "https://example.test/path?q=1"
    assert call["data"] == b"payload"
    assert call["stream"] is True
    assert (b"x-test", b"1") in [(k.lower(), v) for k, v in call["headers"]]


async def test_the_body_is_not_decoded_a_second_time_because_curl_already_did(sessions: list[_Session]) -> None:
    transport, session = _transport(sessions)
    session.result = _CurlResponse(body=b"already plain", headers=[(b"Content-Encoding", b"gzip")])
    async with httpx2.AsyncClient(transport=transport) as client:
        response = await client.get("https://example.test/")
    assert response.content == b"already plain"


async def test_a_streamed_body_arrives_chunk_by_chunk_and_is_closed(sessions: list[_Session]) -> None:
    transport, session = _transport(sessions)
    curl = _CurlResponse(chunks=[b"ab", b"cd"])
    session.result = curl
    async with httpx2.AsyncClient(transport=transport) as client:
        response = await client.get("https://example.test/")
    assert response.content == b"abcd"
    assert curl.closed


async def test_default_headers_drop_httpxs_own_defaults_but_keep_one_the_caller_set(sessions: list[_Session]) -> None:
    transport, session = _transport(sessions, default_headers=True)
    async with httpx2.AsyncClient(transport=transport) as client:
        await client.get("https://example.test/", headers={"Accept": "application/vnd.test+json"})
    (call,) = session.calls
    sent = {k.lower(): v for k, v in call["headers"]}
    assert b"user-agent" not in sent  # httpx2's `python-httpx/x.y` is left for curl's browser profile to supply
    assert sent[b"accept"] == b"application/vnd.test+json"  # the caller's own value stays


async def test_without_default_headers_httpxs_defaults_are_sent(sessions: list[_Session]) -> None:
    transport, session = _transport(sessions, default_headers=False)
    async with httpx2.AsyncClient(transport=transport) as client:
        await client.get("https://example.test/")
    (call,) = session.calls
    assert b"user-agent" in {k.lower() for k, _ in call["headers"]}


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (CurlECode.COULDNT_CONNECT, httpx2.ConnectError),
        (CurlECode.COULDNT_RESOLVE_HOST, httpx2.ConnectError),
        (CurlECode.SSL_CONNECT_ERROR, httpx2.ConnectError),
        (CurlECode.RECV_ERROR, httpx2.ReadError),
        (CurlECode.OPERATION_TIMEDOUT, httpx2.TimeoutException),
        (CurlECode.BAD_CONTENT_ENCODING, httpx2.DecodingError),
    ],
)
async def test_a_curl_error_becomes_the_httpx_error_a_native_transport_raises(
    sessions: list[_Session], code: CurlECode, expected: type[httpx2.RequestError]
) -> None:
    transport, session = _transport(sessions)
    session.result = RequestException("curl said no", code)
    async with httpx2.AsyncClient(transport=transport) as client:
        with pytest.raises(expected):
            await client.get("https://example.test/")


async def test_an_unknown_curl_error_is_still_an_httpx_request_error(sessions: list[_Session]) -> None:
    transport, session = _transport(sessions)
    session.result = RequestException("odd", CurlECode.GOT_NOTHING)
    async with httpx2.AsyncClient(transport=transport) as client:
        with pytest.raises(httpx2.RequestError):
            await client.get("https://example.test/")


async def test_a_method_curl_cannot_send_is_refused_before_any_request(sessions: list[_Session]) -> None:
    transport, session = _transport(sessions)
    async with httpx2.AsyncClient(transport=transport) as client:
        with pytest.raises(httpx2.UnsupportedProtocol):
            await client.request("PROPFIND", "https://example.test/")
    assert session.calls == []


async def test_closing_the_transport_closes_the_curl_session(sessions: list[_Session]) -> None:
    transport, session = _transport(sessions)
    await transport.aclose()
    assert session.closed


def test_import_without_curl_cffi_names_the_extra() -> None:
    import subprocess
    import sys

    script = "import sys\nsys.modules['curl_cffi'] = None\nimport headgate._curl_transport"
    result = subprocess.run([sys.executable, "-I", "-c", script], capture_output=True, text=True, check=False)
    assert result.returncode != 0
    assert "headgate[curl]" in result.stderr
