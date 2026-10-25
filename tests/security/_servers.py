# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""Real local HTTP servers for the security tests: plain HTTP/1.1, and TLS speaking HTTP/2 (or HTTP/1.1 by ALPN).

A server answers every request through a handler and records what reached it: each connection, each request (method,
path, headers, the TLS server name the client sent, the HTTP version), so a test can prove a request was never sent
or went out exactly as pinned. The TLS certificate (``certs/server.pem``) is issued by ``certs/ca.pem`` for
``api.example``, ``localhost`` and ``127.0.0.1``; the CA's key was discarded after signing.
"""

from __future__ import annotations

import asyncio
import contextlib
import ssl
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Final

import h2.config
import h2.connection
import h2.events

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable

#: The certificates the TLS server presents and a client trusts.
CERTS: Final = Path(__file__).parent / "certs"

#: The largest read from a socket at once.
_READ_BYTES: Final = 65536


@dataclass(frozen=True, slots=True)
class Seen:
    """A request as it reached the server."""

    method: str
    path: str
    headers: dict[str, str]
    server_name: str | None
    http_version: str


#: What a handler answers: the status, the headers and the body.
type Answer = tuple[int, dict[str, str], bytes]

type Handler = Callable[[Seen], Answer]


def ok(_seen: Seen) -> Answer:
    """Answer every request with a 200 and a short body."""
    return 200, {}, b"ok"


@dataclass(slots=True)
class LocalServer:
    """A running server's address and what reached it."""

    host: str
    port: int
    scheme: str
    connections: int = 0
    requests: list[Seen] = field(default_factory=list)
    server_names: list[str | None] = field(default_factory=list)

    def url(self, path: str = "/", *, host: str | None = None) -> str:
        """Return the URL of *path* on this server, addressed by *host* (the bound address by default)."""
        return f"{self.scheme}://{host or self.host}:{self.port}{path}"


def client_ssl_context() -> ssl.SSLContext:
    """Return a client TLS context trusting only the test CA."""
    return ssl.create_default_context(cafile=CERTS / "ca.pem")


def _server_ssl_context(server: LocalServer) -> ssl.SSLContext:
    """Return the server TLS context, offering HTTP/2 and recording each client's server name."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(CERTS / "server.pem")
    context.set_alpn_protocols(["h2", "http/1.1"])

    def record(_socket: ssl.SSLObject, name: str | None, _context: ssl.SSLContext) -> None:
        server.server_names.append(name)

    context.sni_callback = record
    return context


async def _serve_http1(
    server: LocalServer, handler: Handler, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, name: str | None
) -> None:
    """Answer HTTP/1.1 requests on one connection until the client closes it (``IncompleteReadError``)."""
    while True:
        head = await reader.readuntil(b"\r\n\r\n")
        request_line, *lines = head.decode("latin-1").split("\r\n")[:-2]
        method, path, _version = request_line.split(" ")
        headers = {key.lower(): value.strip() for key, _, value in (line.partition(":") for line in lines)}
        await reader.readexactly(int(headers.get("content-length", "0")))
        seen = Seen(method, path, headers, name, "HTTP/1.1")
        server.requests.append(seen)
        status, answer_headers, body = handler(seen)
        lines_out = [f"HTTP/1.1 {status} X", f"Content-Length: {len(body)}"]
        lines_out += [f"{key}: {value}" for key, value in answer_headers.items()]
        writer.write(("\r\n".join(lines_out) + "\r\n\r\n").encode("latin-1") + body)
        await writer.drain()


async def _serve_http2(
    server: LocalServer, handler: Handler, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, name: str | None
) -> None:
    """Answer HTTP/2 requests on one connection until the client closes it."""
    connection = h2.connection.H2Connection(h2.config.H2Configuration(client_side=False, header_encoding="utf-8"))
    connection.initiate_connection()
    writer.write(connection.data_to_send())
    while data := await reader.read(_READ_BYTES):
        for event in connection.receive_data(data):
            if isinstance(event, h2.events.RequestReceived):
                headers = {str(key): str(value) for key, value in event.headers or []}
                seen = Seen(headers[":method"], headers[":path"], headers, name, "HTTP/2")
                server.requests.append(seen)
                status, answer_headers, body = handler(seen)
                connection.send_headers(event.stream_id, [(":status", str(status)), *answer_headers.items()])
                connection.send_data(event.stream_id, body, end_stream=True)
        writer.write(connection.data_to_send())
        await writer.drain()


@contextlib.asynccontextmanager
async def serving(handler: Handler = ok, *, tls: bool = False, host: str = "127.0.0.1") -> AsyncGenerator[LocalServer]:
    """Run a server on *host* (an ephemeral port) answering through *handler*, closing every connection on exit."""
    server = LocalServer(host=host, port=0, scheme="https" if tls else "http")
    writers: set[asyncio.StreamWriter] = set()

    async def connected(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        server.connections += 1
        writers.add(writer)
        tls_object = writer.get_extra_info("ssl_object")
        name = server.server_names[-1] if tls_object is not None else None
        serve = _serve_http2 if tls_object is not None and tls_object.selected_alpn_protocol() == "h2" else _serve_http1
        with contextlib.suppress(ConnectionError, asyncio.IncompleteReadError):
            await serve(server, handler, reader, writer, name)
        writer.close()

    listener = await asyncio.start_server(connected, host, 0, ssl=_server_ssl_context(server) if tls else None)
    server.port = listener.sockets[0].getsockname()[1]
    try:
        yield server
    finally:
        listener.close()
        for writer in writers:
            writer.close()
        await listener.wait_closed()
