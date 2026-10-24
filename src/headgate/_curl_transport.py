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
above, as clause 1 requires) and cut down to what headgate calls. The upstream package supports plain ``httpx`` only,
so this copy speaks ``httpx2``, the one HTTP library headgate is built on.

**What was cut.** The sync transport, the ``CurlTransportParams`` bag (``cert``, ``ja3``, ``akamai``, ``extra_fp``,
``curl_infos``, ``proxy``, ``local_address``, ``debug``, ``http_version``), the ``loop`` / ``async_curl`` arguments and
the compatibility shims for ``curl_cffi`` older than the 0.16.2 floor headgate declares. The surface kept is ``impersonate``, ``default_headers`` and ``curl_options``
(for example ``{CurlOpt.FRESH_CONNECT: True}``).

**What was kept, because it is behaviour.**

* ``curl`` is told to ignore the environment's proxy (``NOPROXY="*"``), cookies are discarded and redirects are not
  followed, so the transport stack above it (redirect handling, the breaker, retry) sees every hop and every status.
* Responses are returned in stream mode and curl has already decompressed the body, so httpx2's content decoder is
  replaced with the identity decoder (``curl_cffi`` issue 438).
* ``default_headers=True`` hands the header set to curl's impersonation profile, so httpx2's own defaults
  (``User-Agent: python-httpx``, ``Accept-Encoding``, ...) are removed from the request when the caller left them
  untouched.
* A curl error code maps onto the httpx2 exception a native transport would raise, so the policy layers classify it.
"""

from __future__ import annotations

import functools
from http.cookiejar import Cookie, CookieJar
from typing import TYPE_CHECKING, Any, ClassVar, get_args, override

import httpx2

try:
    from curl_cffi import CurlECode, CurlHttpVersion, CurlOpt
    from curl_cffi.requests import Response as _Response
    from curl_cffi.requests.exceptions import RequestException as _RequestError
    from curl_cffi.requests.session import AsyncSession as _AsyncSession
    from curl_cffi.requests.session import HttpMethod
except ImportError as exc:  # pragma: no cover - exercised in a subprocess
    raise ImportError("AsyncCurlTransport needs the curl extra: pip install 'headgate[curl]'") from exc
from httpx2._decoders import IdentityDecoder

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from curl_cffi.requests.impersonate import BrowserTypeLiteral

__all__ = ["AsyncCurlTransport"]

_CURL_ERRORS: dict[CurlECode, type[httpx2.RequestError]] = {
    CurlECode.UNSUPPORTED_PROTOCOL: httpx2.ProtocolError,
    CurlECode.COULDNT_CONNECT: httpx2.ConnectError,
    CurlECode.COULDNT_RESOLVE_HOST: httpx2.ConnectError,
    CurlECode.COULDNT_RESOLVE_PROXY: httpx2.ProxyError,
    CurlECode.SSL_CONNECT_ERROR: httpx2.ConnectError,
    CurlECode.READ_ERROR: httpx2.ReadError,
    CurlECode.RECV_ERROR: httpx2.ReadError,
    CurlECode.WRITE_ERROR: httpx2.WriteError,
    CurlECode.BAD_CONTENT_ENCODING: httpx2.DecodingError,
    CurlECode.OPERATION_TIMEDOUT: httpx2.TimeoutException,
}

_CURL_HTTP_VERSIONS: dict[CurlHttpVersion, bytes] = {
    CurlHttpVersion.V1_0: b"HTTP/1.0",
    CurlHttpVersion.V1_1: b"HTTP/1.1",
    CurlHttpVersion.V2_0: b"HTTP/2",
    CurlHttpVersion.V3: b"HTTP/3",
}


_METHODS: dict[str, HttpMethod] = {method: method for method in get_args(HttpMethod)}


@functools.cache
def _httpx_default_headers() -> tuple[tuple[str, str], ...]:
    """The headers httpx2 adds to a request the caller gave none, read from a client rather than hard-coded."""
    return tuple(httpx2.Client(trust_env=False).headers.items())


class _DiscardingCookieJar(CookieJar):
    """Keeps no cookie: the transport stack above owns state, and a fresh handshake per request is the point."""

    @override
    def set_cookie(self, cookie: Cookie) -> None:
        return


class _CurlByteStream(httpx2.AsyncByteStream):
    def __init__(self, response: _Response) -> None:
        self._response = response

    @override
    async def __aiter__(self) -> AsyncIterator[bytes]:
        if not self._response.queue:  # stream mode off: the body is already whole
            yield self._response.content
            return
        async for chunk in self._response.aiter_content():
            yield bytes(chunk)  # curl_cffi leaves the chunk untyped; `bytes(bytes)` returns the same object, no copy

    @override
    async def aclose(self) -> None:
        await self._response.aclose()


class AsyncCurlTransport(httpx2.AsyncBaseTransport):
    """An ``httpx2`` async transport that sends each request through ``curl_cffi`` with a browser's TLS handshake.

    :param impersonate: the ``curl_cffi`` browser profile; ``"chrome"`` is a rolling alias that each ``curl_cffi``
        release re-points at a newer Chrome, which is why ``curl_cffi`` carries a floor in headgate's manifest.
    :param default_headers: let the profile supply the browser's headers (and drop httpx2's) instead of sending none.
    :param curl_options: extra libcurl options, e.g. ``{CurlOpt.FRESH_CONNECT: True}`` to open a new connection per
        request for hosts that fingerprint a reused one.
    :param max_connections: concurrent curl handles.
    """

    _stream: ClassVar[bool] = True

    def __init__(
        self,
        *,
        impersonate: BrowserTypeLiteral,
        default_headers: bool = False,
        curl_options: dict[CurlOpt, Any] | None = None,
        max_connections: int = 10,
    ) -> None:
        # curl reads the environment's proxy settings on its own; httpx2 expects a transport to ignore them
        # (https://github.com/lexiforest/curl_cffi/issues/345), so they are switched off explicitly.
        options: dict[CurlOpt, Any] = {**(curl_options or {}), CurlOpt.NOPROXY: "*"}
        self._default_headers = default_headers
        self._session = _AsyncSession(
            max_clients=max_connections,
            impersonate=impersonate,
            default_headers=default_headers,
            curl_options=options,
            trust_env=False,
            cookies=_DiscardingCookieJar(),
            allow_redirects=False,
        )

    @override
    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        await request.aread()
        if self._default_headers:
            for name, value in _httpx_default_headers():
                if request.headers.get(name) == value:
                    request.headers.pop(name, None)
        timeout = request.extensions["timeout"]
        method = _METHODS.get(request.method)
        if method is None:
            raise httpx2.UnsupportedProtocol(f"curl cannot send an HTTP {request.method} request", request=request)
        try:
            response = await self._session.request(
                method=method,
                url=str(request.url),
                data=request.content,
                headers=request.headers.raw,
                # curl has a connect timeout and an overall one; httpx2's `write` and `pool` have no curl twin.
                timeout=(timeout["connect"], timeout["read"]),
                stream=self._stream,
            )
        except _RequestError as exc:
            raise _httpx_error(request, exc) from exc
        return _httpx_response(request, response)

    @override
    async def aclose(self) -> None:
        await self._session.close()


def _httpx_response(request: httpx2.Request, response: _Response) -> httpx2.Response:
    # curl_cffi leaves `response.request.headers` empty (issue 368); when it has them they are what went on the wire.
    if response.request and response.request.headers:
        request.headers = httpx2.Headers([(k, v or b"") for k, v in response.request.headers.raw])
    out = httpx2.Response(
        status_code=response.status_code,
        request=request,
        headers=httpx2.Headers([(k, v or b"") for k, v in response.headers.raw]),
        stream=_CurlByteStream(response),
        extensions={"curl": {"response": response, "infos": response.infos}},
    )
    # curl has already decompressed the body and exposes no raw one (issue 438), so httpx2 must not decode it again.
    out._decoder = IdentityDecoder()
    http_version = CurlHttpVersion(response.http_version)
    if http_version in _CURL_HTTP_VERSIONS:
        out.extensions["http_version"] = _CURL_HTTP_VERSIONS[http_version]
    if response.reason:  # empty under HTTP/2 and HTTP/3
        out.extensions["reason_phrase"] = response.reason.encode("ascii", "ignore")
    out.elapsed = response.elapsed
    return out


def _httpx_error(request: httpx2.Request, exc: _RequestError) -> httpx2.RequestError:
    # The curl error code is lost here; the message and the class are what the policy layers read.
    return _CURL_ERRORS.get(exc.code, httpx2.RequestError)(str(exc.args[0]), request=request)
