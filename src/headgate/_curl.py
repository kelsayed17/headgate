# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT
#
# This file is derived from httpx-curl-cffi 0.1.5 (https://github.com/vgavro/httpx-curl-cffi), under its license:
#
# BSD 3-Clause License
#
# Copyright (c) 2025, Victor Gavro
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
# 1. Redistributions of source code must retain the above copyright notice, this
#    list of conditions and the following disclaimer.
#
# 2. Redistributions in binary form must reproduce the above copyright notice,
#    this list of conditions and the following disclaimer in the documentation
#    and/or other materials provided with the distribution.
#
# 3. Neither the name of the copyright holder nor the names of its
#    contributors may be used to endorse or promote products derived from
#    this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
# DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
# FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
# DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
# SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
# CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
# OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
# OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

"""The ``curl_cffi`` transport for hosts fronted by TLS-fingerprinting CDNs (the ``headgate[curl]`` extra).

Vendored from ``httpx-curl-cffi`` 0.1.5 (BSD-3-Clause, Copyright (c) 2025 Victor Gavro; the licence is the comment block
above, as clause 1 requires) and cut down to what headgate calls. The upstream package speaks plain ``httpx``; this copy
speaks ``httpx2``. ``curl_cffi`` itself ships no httpx or httpx2 transport.

The kept surface is ``impersonate``, ``default_headers``, ``curl_options`` and ``max_connections``. The sync transport,
the fingerprint and proxy parameter bag and the shims for ``curl_cffi`` older than the declared floor are not kept.

The transport behaves like a native httpx2 one wherever the layers above it can tell:

* curl ignores the environment's proxy (``NOPROXY="*"``), keeps no cookie and follows no redirect, so the stack above
  it (redirect handling, the breaker, retry) sees every hop and every status.
* curl decompresses the body itself and exposes no raw one (``curl_cffi`` issue 438). The response therefore carries no
  ``Content-Encoding`` or ``Content-Length``: its stream already holds the decoded bytes, so httpx2 picks its identity
  decoder and a cache that stores the stream replays it correctly. The headers as received stay on the ``curl_cffi``
  response, in ``response.extensions["curl"]["response"]``.
* ``default_headers=True`` hands the header set to curl's impersonation profile, so httpx2's own defaults
  (``User-Agent: python-httpx2/...``, ``Accept-Encoding``, ...) and the ``User-Agent`` a client sends without
  ``HttpOptions(user_agent=...)`` are removed from the request when the caller left them untouched.
* A curl error code, raised while sending or while streaming the body, maps onto the httpx2 exception a native
  transport would raise, so the policy layers classify it.
* Closing a response before its body is read aborts the transfer. ``curl_cffi``'s own ``aclose`` waits for the whole
  body to arrive into an unbounded queue, which would defeat headgate's response size cap.
"""

from __future__ import annotations

import asyncio
import functools
from typing import TYPE_CHECKING, Any, get_args, override

import httpx2

from headgate._user_agent import DEFAULT_USER_AGENT

try:
    from curl_cffi import CurlECode, CurlHttpVersion, CurlOpt
    from curl_cffi.requests.exceptions import RequestException as _RequestError
    from curl_cffi.requests.session import AsyncSession as _AsyncSession
    from curl_cffi.requests.session import HttpMethod
except ImportError as exc:
    msg = "AsyncCurlTransport needs the curl extra: pip install 'headgate[curl]'"
    raise ImportError(msg) from exc

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping

    from curl_cffi.requests import Response as _Response
    from curl_cffi.requests.impersonate import BrowserTypeLiteral

__all__ = ["AsyncCurlTransport"]

#: The httpx2 exception a native transport raises for the same failure. A code missing here becomes a plain
#: :class:`httpx2.TransportError`; ``OPERATION_TIMEDOUT`` is split by :func:`_timeout_error`. ``READ_ERROR`` and
#: ``WRITE_ERROR`` are absent on purpose: in libcurl they are local callback failures, not network ones
#: (``WRITE_ERROR`` is what an aborted transfer reports).
_CURL_ERRORS: dict[int, type[httpx2.TransportError | httpx2.DecodingError]] = {
    CurlECode.UNSUPPORTED_PROTOCOL: httpx2.UnsupportedProtocol,
    CurlECode.COULDNT_RESOLVE_PROXY: httpx2.ProxyError,
    CurlECode.PROXY: httpx2.ProxyError,
    CurlECode.COULDNT_RESOLVE_HOST: httpx2.ConnectError,
    CurlECode.COULDNT_CONNECT: httpx2.ConnectError,
    CurlECode.QUIC_CONNECT_ERROR: httpx2.ConnectError,
    CurlECode.SSL_CONNECT_ERROR: httpx2.ConnectError,
    CurlECode.PEER_FAILED_VERIFICATION: httpx2.ConnectError,
    CurlECode.SEND_ERROR: httpx2.WriteError,
    CurlECode.RECV_ERROR: httpx2.ReadError,
    CurlECode.GOT_NOTHING: httpx2.RemoteProtocolError,
    CurlECode.WEIRD_SERVER_REPLY: httpx2.RemoteProtocolError,
    CurlECode.PARTIAL_FILE: httpx2.RemoteProtocolError,
    CurlECode.HTTP2: httpx2.RemoteProtocolError,
    CurlECode.HTTP2_STREAM: httpx2.RemoteProtocolError,
    CurlECode.HTTP3: httpx2.RemoteProtocolError,
    CurlECode.BAD_CONTENT_ENCODING: httpx2.DecodingError,
}

#: libcurl's messages for a timeout before the connection is up. Message text is the only signal: libcurl reports every
#: timeout as ``OPERATION_TIMEDOUT``, and the failed handle is reset before the error reaches Python.
_CONNECT_TIMEOUT_MESSAGES = ("Resolving timed out", "Connection timed out", "Failed to connect to")

#: ``CURLINFO_HTTP_VERSION`` values, as httpx2's ``http_version`` extension spells them.
_HTTP_VERSIONS: dict[int, bytes] = {
    CurlHttpVersion.V1_0: b"HTTP/1.0",
    CurlHttpVersion.V1_1: b"HTTP/1.1",
    CurlHttpVersion.V2_0: b"HTTP/2",
    CurlHttpVersion.V3: b"HTTP/3",
}

#: The response headers that describe the body as it was on the wire, which curl has already decoded.
_WIRE_ENCODING_HEADERS = ("content-encoding", "content-length")

_METHODS: dict[str, HttpMethod] = {method: method for method in get_args(HttpMethod)}


class AsyncCurlTransport(httpx2.AsyncBaseTransport):
    """Send each request through ``curl_cffi`` with a browser's TLS handshake, as an ``httpx2`` async transport.

    :param impersonate: the ``curl_cffi`` browser profile; ``"chrome"`` is a rolling alias that each ``curl_cffi``
        release re-points at a newer Chrome, which is why ``curl_cffi`` carries a floor in headgate's manifest.
    :param default_headers: let the profile supply the browser's headers (and drop httpx2's) instead of sending none.
    :param curl_options: extra libcurl options, e.g. ``{CurlOpt.FRESH_CONNECT: True}`` to open a new connection per
        request for hosts that fingerprint a reused one. Values are whatever libcurl takes for the option (``curl_cffi``
        annotates them as ``str``, which is narrower than what it accepts).
    :param max_connections: concurrent curl handles; a request beyond them waits for one, with no pool timeout.
    """

    def __init__(
        self,
        *,
        impersonate: BrowserTypeLiteral,
        default_headers: bool = False,
        curl_options: Mapping[CurlOpt, Any] | None = None,
        max_connections: int = 10,
    ) -> None:
        # curl reads the environment's proxy settings on its own, and an httpx2 transport ignores them
        # (https://github.com/lexiforest/curl_cffi/issues/345).
        options: dict[CurlOpt, Any] = {**(curl_options or {}), CurlOpt.NOPROXY: "*"}
        self._default_headers = default_headers
        self._session = _AsyncSession(
            max_clients=max_connections,
            impersonate=impersonate,
            default_headers=default_headers,
            curl_options=options,
            trust_env=False,
            discard_cookies=True,
            allow_redirects=False,
        )

    @override
    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        method = _METHODS.get(request.method)
        if method is None:
            msg = f"curl cannot send an HTTP {request.method} request"
            raise httpx2.UnsupportedProtocol(msg, request=request)
        await request.aread()
        if self._default_headers:
            for name, value in _httpx_default_headers():
                if request.headers.get(name) == value:
                    del request.headers[name]
        try:
            response = await self._session.request(
                method=method,
                url=str(request.url),
                data=request.content,
                headers=request.headers.raw,
                timeout=_curl_timeout(request.extensions.get("timeout", {})),
                stream=True,
            )
        except _RequestError as exc:
            raise _httpx_error(request, exc) from exc
        return _httpx_response(request, response)

    @override
    async def aclose(self) -> None:
        await self._session.close()


def _httpx_response(request: httpx2.Request, response: _Response) -> httpx2.Response:
    """Wrap the streamed ``curl_cffi`` *response* as the httpx2 response a native transport returns.

    ``elapsed`` is left to httpx2, which measures until the body is closed; curl's own figure stops at the headers.
    """
    headers = httpx2.Headers([(k, v or b"") for k, v in response.headers.raw])
    if "content-encoding" in headers:
        for name in _WIRE_ENCODING_HEADERS:
            headers.pop(name, None)
    extensions: dict[str, object] = {"curl": {"response": response, "infos": response.infos}}
    if (http_version := _HTTP_VERSIONS.get(response.http_version)) is not None:
        extensions["http_version"] = http_version
    if response.reason:  # empty under HTTP/2 and HTTP/3
        extensions["reason_phrase"] = response.reason.encode("ascii", "ignore")
    return httpx2.Response(
        status_code=response.status_code,
        request=request,
        headers=headers,
        stream=_CurlByteStream(request, response),
        extensions=extensions,
    )


class _CurlByteStream(httpx2.AsyncByteStream):
    """The body of a streamed ``curl_cffi`` response, with curl's errors raised as httpx2 ones."""

    def __init__(self, request: httpx2.Request, response: _Response) -> None:
        self._request = request
        self._response = response
        # Set to make curl's write callback abort the transfer. Only a streamed response has one; for any other there
        # is nothing left to abort, and a fresh event is never read.
        self._abort: asyncio.Event = response.quit_now or asyncio.Event()

    @override
    async def __aiter__(self) -> AsyncIterator[bytes]:
        try:
            async for chunk in self._response.aiter_content():
                yield bytes(chunk)  # `curl_cffi` leaves the chunk untyped; `bytes(bytes)` returns it without a copy
        except _RequestError as exc:
            raise _httpx_error(self._request, exc) from exc

    @override
    async def aclose(self) -> None:
        # Without the abort, `aclose` waits for (and queues) the rest of the body.
        self._abort.set()
        await self._response.aclose()


def _httpx_error(request: httpx2.Request, exc: _RequestError) -> httpx2.RequestError:
    """Return the httpx2 exception a native transport raises for the curl failure *exc*."""
    message = str(exc)
    if exc.code == CurlECode.OPERATION_TIMEDOUT:
        kind: type[httpx2.RequestError] = _timeout_error(message)
    else:
        kind = _CURL_ERRORS.get(exc.code, httpx2.TransportError)
    return kind(message, request=request)


def _timeout_error(message: str) -> type[httpx2.TimeoutException]:
    """Return :class:`httpx2.ConnectTimeout` for a timeout before the connection was up, else ``ReadTimeout``."""
    if any(marker in message for marker in _CONNECT_TIMEOUT_MESSAGES):
        return httpx2.ConnectTimeout
    return httpx2.ReadTimeout


def _curl_timeout(timeout: Mapping[str, float | None]) -> tuple[float, float] | None:
    """Return the ``(connect, read)`` pair curl takes for httpx2's *timeout* extension.

    curl has a connect timeout and, in stream mode, a stall window of ``connect + read`` seconds; httpx2's ``write`` and
    ``pool`` have no curl twin. A ``None`` (no limit) becomes curl's own "no limit" (``0``, or no tuple at all for an
    unlimited read), which never makes a limit tighter than the one asked for. A missing key means no limit too: a
    transport above may rebuild the request without the extension, as hishel does.
    """
    read = timeout.get("read")
    if read is None:
        return None
    return (timeout.get("connect") or 0.0, read)


@functools.cache
def _httpx_default_headers() -> tuple[tuple[str, str], ...]:
    """Return the headers httpx2 and headgate add to a request the caller gave none.

    httpx2's are read from a client rather than hard-coded; headgate's is the ``User-Agent`` of a client without a
    product token of its own.
    """
    with httpx2.Client(trust_env=False) as client:
        return (*client.headers.items(), ("user-agent", DEFAULT_USER_AGENT))
