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

Vendored from ``httpx-curl-cffi`` 0.1.5 (BSD-3-Clause, Copyright (c) 2025 Victor Gavro; the license is the comment block
above, as clause 1 requires) and cut down to what headgate calls. The upstream package speaks plain ``httpx``; this copy
speaks ``httpx2``. ``curl_cffi`` itself ships no httpx or httpx2 transport.

The kept surface is ``impersonate``, ``default_headers``, ``curl_options`` and ``max_connections``. The sync transport,
the fingerprint and proxy parameter bag and the shims for ``curl_cffi`` older than the declared floor are not kept.

The transport behaves like a native httpx2 one wherever the layers above it can tell:

* curl ignores the environment's proxy (``NOPROXY="*"``, or ``""`` beside an explicit ``CurlOpt.PROXY``, so that proxy
  serves every host), keeps no cookie and follows no redirect, so the stack above it (redirect handling, the breaker,
  retry) sees every hop and every status.
* curl verifies TLS against certifi's bundle, which no ``SSL_CERT_FILE``, ``CURL_CA_BUNDLE`` or ``REQUESTS_CA_BUNDLE``
  replaces; a ``CurlOpt.CAINFO`` in ``curl_options`` does. libcurl itself still writes the TLS session keys to the file
  ``SSLKEYLOGFILE`` names, when that variable is set.
* A request the retry layer would not send again (a method outside the call's idempotent ones, without an idempotency
  key) goes out on a fresh connection, because libcurl re-sends a request whose reused connection died unanswered. A
  ``CurlOpt.FRESH_CONNECT`` in ``curl_options`` is applied after it and wins: ``False`` there lets libcurl re-send.
* The response is returned at its head, before any body byte, and a call cancelled before then removes its transfer.
* curl decompresses the body itself and exposes no raw one (``curl_cffi`` issue 438). The response therefore carries no
  ``Content-Encoding`` or ``Content-Length``: its stream already holds the decoded bytes, so httpx2 picks its identity
  decoder and a cache that stores the stream replays it correctly. The headers as received stay on the ``curl_cffi``
  response, in ``response.extensions["curl"]["response"]``.
* ``default_headers=True`` hands the header set to curl's impersonation profile, so httpx2's own defaults
  (``User-Agent: python-httpx2/...``, ``Accept-Encoding``, ...) and the ``User-Agent`` a client sends without
  ``HttpOptions(user_agent=...)`` are removed from the request when the caller left them untouched.
* A curl error code, raised while sending or while streaming the body, maps onto the httpx2 exception a native
  transport would raise, so the policy layers classify it.
* Closing a response before its body is read aborts the transfer, even a stalled one. ``curl_cffi``'s own ``aclose``
  waits for the whole body to arrive into an unbounded queue, which would defeat headgate's response size cap.

**The SSRF guard** (``SecurityOptions(block_private_hosts=True)``) is bound to the transport when a client is built on
it (:class:`GuardBinding`). Each request's host is then resolved and vetted through the
guard, and curl is handed every vetted address as a ``CURLOPT_RESOLVE`` entry, so curl connects to them and never looks
the name up itself; the URL keeps its name, so TLS still sends it as SNI and verifies the certificate against it.
libcurl keeps a resolve entry in the session's DNS cache after the transfer, and its connection pool is shared by every
request, so one transport serves one guard: a client built on it with another guard, or none, is refused. A
``CurlOpt.PROXY``, ``PRE_PROXY``, ``RESOLVE``, ``CONNECT_TO``, ``UNIX_SOCKET_PATH`` or ``ABSTRACT_UNIX_SOCKET`` in
``curl_options`` would choose the address itself, so the guard refuses those too.

**Clients that share a transport share its session**, so a client's close does not close it while another client
built on it is open: each sends through a hold on the transport, and the last hold let go closes it. The transport's
own ``aclose`` closes the session at once. A client built on a closed transport is refused where it is built, since
each of its sends would fail.
"""

import asyncio
import functools
import re
import threading
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Final, cast, final, get_args, override

import httpx2

from headgate._core import CallContext, check_int, check_kind, sealed
from headgate._user_agent import DEFAULT_USER_AGENT
from headgate.retry._policy import IDEMPOTENT_METHODS, RETRY_POLICY, is_keyed
from headgate.security._addresses import AddressGuard
from headgate.security._transport import vet_request

try:
    import certifi
    from curl_cffi import Curl, CurlECode, CurlHttpVersion, CurlInfo, CurlOpt
    from curl_cffi.requests.exceptions import RequestException as _RequestError
    from curl_cffi.requests.session import AsyncSession as _AsyncSession
    from curl_cffi.requests.session import HttpMethod
except ImportError as exc:
    msg = "AsyncCurlTransport needs the curl extra: pip install 'headgate[curl]'"
    raise ImportError(msg) from exc

from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from io import BytesIO

from curl_cffi.requests import Response as _Response
from curl_cffi.requests.impersonate import BrowserTypeLiteral

__all__ = ["AsyncCurlTransport", "GuardBinding", "guard_binding"]

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

#: What ``curl_options=`` takes, for the refusal of anything else.
_CURL_OPTIONS_WANTED = "a mapping of CurlOpt to values, or None"

#: The curl options that choose the address a connection reaches, which the SSRF guard must choose instead: a proxy
#: resolves the target itself, a resolve or connect-to entry of the caller's would replace the vetted one, and a Unix
#: socket replaces the host's address altogether.
_ADDRESS_OPTIONS = (
    CurlOpt.PROXY,
    CurlOpt.PRE_PROXY,
    CurlOpt.RESOLVE,
    CurlOpt.CONNECT_TO,
    CurlOpt.UNIX_SOCKET_PATH,
    CurlOpt.ABSTRACT_UNIX_SOCKET,
    # The pin covers a host on the URL's port: another port is one curl resolves for itself.
    CurlOpt.PORT,
)


@dataclass
class _Loan:
    """One request's claim on a curl handle: a fresh connection or not, its vetted addresses, and the handle it holds.

    ``resolve`` is the ``CURLOPT_RESOLVE`` entry pinning the request to the addresses the SSRF guard vetted, if on.
    """

    fresh: bool
    resolve: str | None = None
    curl: Curl | None = None


#: The request whose ``curl_cffi`` call is running. ``curl_cffi`` takes the handle inside that call, and calls back from
#: the call's task or a task it starts there, which sees the same loan.
_LOAN: ContextVar[_Loan] = ContextVar("headgate_curl_loan")


class _Curl(Curl):
    """A curl handle that opens a streamed response at the end of its head, not at its first body byte."""

    _body: Callable[[bytes], object]

    @override
    def setopt(self, option: CurlOpt, value: Any) -> int:
        # `curl_cffi` sets the stream's write callback, which opens the response on its first call, before the
        # header buffer.
        if option == CurlOpt.WRITEFUNCTION:
            self._body = value
        elif option == CurlOpt.HEADERDATA:
            return super().setopt(CurlOpt.HEADERFUNCTION, functools.partial(self._header, value))
        return super().setopt(option, value)

    def _header(self, buffer: BytesIO, line: bytes) -> int:
        """Keep a header *line* in *buffer*; at the end of a final (not 1xx, not a proxy's) head, open the stream."""
        buffer.write(line)
        if line in {b"\r\n", b"\n"} and cast("int", self.getinfo(CurlInfo.RESPONSE_CODE)) >= httpx2.codes.OK:
            self._body(b"")
        return len(line)


class _Session(_AsyncSession):
    """``curl_cffi``'s session, lending each request its own :class:`_Curl` through :data:`_LOAN`."""

    @override
    async def pop_curl(self) -> Curl:
        curl = await self.pool.get() or _Curl(debug=self.debug)
        loan = _LOAN.get()
        loan.curl = curl
        # Set after the await, so no other request sees them; `release_curl` resets them.
        if loan.fresh:
            curl.setopt(CurlOpt.FRESH_CONNECT, 1)
        if loan.resolve is not None:
            curl.setopt(CurlOpt.RESOLVE, [loan.resolve])
        return curl

    @override
    def release_curl(self, curl: Curl) -> None:
        _LOAN.get().curl = None
        super().release_curl(curl)


class GuardBinding:
    """The SSRF guard an :class:`AsyncCurlTransport` serves: one, or none, set when the first client is built on it.

    curl keeps one DNS cache and one connection pool for every request it sends, so the transport serves one guard:
    clients with the same guard may share it, and no other client may. A shallow copy of the transport shares its curl
    session, and so its binding. *address_options* names the transport's ``curl_options`` that choose the address curl
    connects to, which the guard must choose instead. Clients on several threads may be built on one transport, so
    :meth:`bind` checks and sets under a lock.

    The clients sharing the transport share its session too, so each client built on it sends through a hold on it
    (:meth:`hold`), and the session is closed when the last of them closes, not the first. :attr:`closed` says it is,
    for every copy of the transport, which shares the session.
    """

    __slots__ = ("_address_options", "_bound", "_holds", "_lock", "closed", "guard")

    def __init__(self, address_options: Sequence[str] = ()) -> None:
        """Start unbound and open, with the names of the transport's own *address_options*, if it has any."""
        self._address_options = tuple(address_options)
        self._bound = False
        self._holds = 0
        self._lock = threading.Lock()
        self.guard: AddressGuard | None = None
        self.closed = False

    def hold(self, transport: httpx2.AsyncBaseTransport) -> httpx2.AsyncBaseTransport:
        """Return a client's hold on *transport*: it sends on the transport, and closing it lets go of it.

        The hold let go last closes *transport*, and so its curl session; the transport's own ``aclose`` still closes
        it at once.
        """
        with self._lock:
            self._holds += 1
        return _Hold(transport, self)

    def release(self) -> bool:
        """Let go of one hold; return whether it was the last, so the transport is to be closed."""
        with self._lock:
            self._holds -= 1
            return self._holds == 0

    def bind(self, guard: AddressGuard | None) -> None:
        """Serve only requests the SSRF *guard* vets (``None``: an unguarded client), pinning each to its addresses.

        :raises ValueError: when the transport is closed, so a client built on it could send nothing; when it already
            serves another guard (or none, or one where *guard* is none); or when *guard* is given and
            ``curl_options`` choose the connection's address (a proxy, a resolve or a connect-to entry, a Unix socket).
        """
        with self._lock:
            if self.closed:
                msg = (
                    "this AsyncCurlTransport is closed (by the last client built on it, or by its aclose()), so every "
                    "request sent on it would fail: build a new one for this client"
                )
                raise ValueError(msg)
            if self._bound and guard != self.guard:
                msg = (
                    "this AsyncCurlTransport already serves a client with a different SSRF guard "
                    "(SecurityOptions(block_private_hosts=..., resolver=..., check=...)): curl shares its DNS cache "
                    "and connections across requests, so give each client its own transport"
                )
                raise ValueError(msg)
            if guard is not None and self._address_options:
                names = ", ".join(self._address_options)
                msg = (
                    f"block_private_hosts=True cannot be combined with curl_options {names}: "
                    "they choose the address curl connects to, which the SSRF guard must choose"
                )
                raise ValueError(msg)
            self._bound = True
            self.guard = guard


class _Hold(httpx2.AsyncBaseTransport):
    """One client's hold on a curl transport other clients may share (see :meth:`GuardBinding.hold`)."""

    def __init__(self, transport: httpx2.AsyncBaseTransport, binding: GuardBinding) -> None:
        """Send on *transport*, letting go of *binding*'s hold on close."""
        self._transport = transport
        self._binding = binding

    @override
    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        return await self._transport.handle_async_request(request)

    @override
    async def aclose(self) -> None:
        """Let go of the hold, and close the transport when it was the last."""
        if self._binding.release():
            await self._transport.aclose()


#: A byte no header line may hold: curl writes a value as it is given, so a line break in one starts another header.
_UNSENDABLE: Final = re.compile(rb"[\r\n\0]")


def _refuse_unsendable_headers(request: httpx2.Request) -> None:
    """Refuse *request* when a header's name or value holds a line break or a NUL, as httpx2's own transport does.

    The options that set a header refuse one where it is given; a value set on the way (a minted token, a middleware's
    or a signer's stamp, an idempotency key) reaches here. The value is not quoted, since it may be a credential.

    :raises httpx2.LocalProtocolError: naming the header, before anything is sent.
    """
    for name, value in request.headers.raw:
        if _UNSENDABLE.search(name) or _UNSENDABLE.search(value):
            msg = f"the header {name.decode('latin-1')!r} holds a line break or a NUL, which no request can send"
            raise httpx2.LocalProtocolError(msg, request=request)


def guard_binding(transport: httpx2.AsyncBaseTransport) -> GuardBinding | None:
    """Return the guard binding *transport* holds as its own, or ``None`` for a transport that resolves names itself.

    Read from the transport's own attributes (``vars``), so a wrapper that forwards attribute lookups to a curl
    transport has none, and a transport without an instance dictionary (one with slots) has none either.
    """
    try:
        found = vars(transport).get("_guard")
    except TypeError:
        return None
    return found if isinstance(found, GuardBinding) else None


@final
@sealed(
    "change each request in a middleware (Client(middleware=...)), which runs on every attempt; a transport of your "
    "own that sends through it is not pinned by the SSRF guard, which only vets each hop before it is sent"
)
class AsyncCurlTransport(httpx2.AsyncBaseTransport):
    """Send each request through ``curl_cffi`` with a browser's TLS handshake, as an ``httpx2`` async transport.

    :param impersonate: the ``curl_cffi`` browser profile; ``"chrome"`` is a rolling alias that each ``curl_cffi``
        release re-points at a newer Chrome, which is why ``curl_cffi`` carries a floor in headgate's manifest.
    :param default_headers: let the profile supply the browser's headers (and drop httpx2's) instead of sending none.
    :param curl_options: extra libcurl options, e.g. ``{CurlOpt.FRESH_CONNECT: True}`` to open a new connection per
        request for hosts that fingerprint a reused one. Values are whatever libcurl takes for the option (``curl_cffi``
        annotates them as ``str``, which is narrower than what it accepts). ``CurlOpt.FOLLOWLOCATION`` is refused with a
        ``ValueError``: curl would follow a redirect below every hook, so headgate follows them itself.
    :param max_connections: concurrent curl handles; a request beyond them waits for one, with no pool timeout.
    :raises TypeError: for an option of the wrong kind: an *impersonate* that is not a ``str``, a *default_headers*
        that is not a ``bool``, *curl_options* that are not a mapping or have a key that is not a ``CurlOpt`` (its name
        or its number included), and a *max_connections* that is not an ``int``.
    :raises ValueError: for a *max_connections* below 1.
    """

    def __init__(
        self,
        *,
        impersonate: BrowserTypeLiteral,
        default_headers: bool = False,
        curl_options: Mapping[CurlOpt, Any] | None = None,
        max_connections: int = 10,
    ) -> None:
        check_kind("impersonate", impersonate, str, "a curl_cffi browser profile, such as 'chrome'")
        check_kind("default_headers", default_headers, bool, "a bool")
        check_kind("curl_options", curl_options, (Mapping, type(None)), _CURL_OPTIONS_WANTED)
        for option in curl_options or {}:
            if not isinstance(option, CurlOpt):
                got = f"the key {option!r}, of type {type(option).__name__}"
                msg = f"curl_options= takes {_CURL_OPTIONS_WANTED} (got {got})"
                raise TypeError(msg)
        check_int("max_connections", max_connections, "a number of curl handles, an int of at least 1")
        if max_connections < 1:
            msg = f"max_connections= takes a number of curl handles, an int of at least 1 (got {max_connections})"
            raise ValueError(msg)
        # curl reads the environment's proxy settings on its own, and an httpx2 transport ignores them
        # (https://github.com/lexiforest/curl_cffi/issues/345).
        # `""` overrides a `NO_PROXY` the environment sets, as `"*"` overrides its `HTTP_PROXY`.
        # The CA bundle is set per request after the ones `curl_cffi` takes from the environment.
        given = curl_options or {}
        if given.get(CurlOpt.FOLLOWLOCATION):
            msg = (
                "curl_options must not set CurlOpt.FOLLOWLOCATION: curl would follow a redirect below every hook, "
                "the credential guard included; headgate follows redirects itself (HttpOptions(follow_redirects=...))"
            )
            raise ValueError(msg)
        options: dict[CurlOpt, Any] = {
            CurlOpt.NOPROXY: "" if CurlOpt.PROXY in given else "*",
            CurlOpt.CAINFO: certifi.where(),
            CurlOpt.PROXY_CAINFO: certifi.where(),
            **given,
        }
        self._default_headers = default_headers
        self._guard = GuardBinding([option.name for option in _ADDRESS_OPTIONS if option in given])
        self._session = _Session(
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
        _refuse_unsendable_headers(request)
        # Vetted before the body is read, so a lookup that fails leaves a one-shot body unread, and the call retryable.
        guard = self._guard.guard
        resolve = None if guard is None else _resolve_entry(request, await vet_request(guard, request))
        # Read from the stream on every send, as a native transport does: `aread()` would keep the first read and send
        # it again, hiding a one-shot body sent twice.
        # shortcut: the body counts as written before curl connects, so an unkeyed POST that never connected is not
        # retried; stream it to curl once curl_cffi's read-callback errors are tested to reach the caller.
        body = b"".join([chunk async for chunk in cast("httpx2.AsyncByteStream", request.stream)])
        if self._default_headers:
            for name, value in _httpx_default_headers():
                if request.headers.get(name) == value:
                    del request.headers[name]
        loan = _Loan(fresh=not _replay_safe(request), resolve=resolve)
        token = _LOAN.set(loan)
        try:
            response = await self._session.request(
                method=method,
                url=str(request.url),
                data=body,
                headers=request.headers.raw,
                timeout=_curl_timeout(request.extensions.get("timeout", {})),
                stream=True,
            )
        except _RequestError as exc:
            raise _httpx_error(request, exc) from exc
        except BaseException:
            # Cancelled before the head: `curl_cffi` would keep the transfer running, and its handle taken.
            if loan.curl is not None:
                self._session.acurl.remove_handle(loan.curl)
            raise
        finally:
            _LOAN.reset(token)
        return _httpx_response(request, response)

    @override
    async def aclose(self) -> None:
        """Close the curl session at once, though clients built on the transport may still hold it."""
        # The last client's hold closes it again later; `curl_cffi` 0.16 crashes closing a closed session.
        if self._guard.closed:
            return
        self._guard.closed = True
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

    @override
    async def __aiter__(self) -> AsyncIterator[bytes]:
        try:
            async for chunk in self._response.aiter_content():
                if chunk:  # the empty chunk that opened the stream at its head
                    yield bytes(chunk)  # `curl_cffi` leaves the chunk untyped; `bytes(bytes)` returns it without a copy
        except _RequestError as exc:
            raise _httpx_error(self._request, exc) from exc

    @override
    async def aclose(self) -> None:
        # `curl_cffi`'s `aclose` waits for (and queues) the rest of the body, and its `quit_now` is read only when a
        # chunk arrives. Cancelling the transfer's task removes its handle, stalled or not.
        task = self._response.astream_task
        if isinstance(task, asyncio.Task):
            task.cancel()
            await asyncio.wait([task])


def _resolve_entry(request: httpx2.Request, addresses: tuple[str, ...]) -> str:
    """Return the ``CURLOPT_RESOLVE`` entry pinning *request*'s host and port to *addresses*, IPv6 ones bracketed."""
    url = request.url
    pinned = ",".join(map(_bracketed, addresses))
    return f"{_bracketed(url.raw_host.decode('ascii'))}:{url.origin.port}:{pinned}"


def _bracketed(address: str) -> str:
    """Return *address* as curl reads it in a resolve entry: an IPv6 address in brackets, any other as it is."""
    return f"[{address}]" if ":" in address else address


def _replay_safe(request: httpx2.Request) -> bool:
    """Return whether the retry layer may send *request* again, as libcurl does on its own when a connection dies.

    That is a method the call's policy treats as idempotent, or a request carrying an idempotency key.
    """
    context = CallContext.of(request)
    policy = context.get(RETRY_POLICY)
    methods = IDEMPOTENT_METHODS if policy is None else policy.methods
    return request.method in methods or is_keyed(context, request)


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
