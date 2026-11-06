# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""Per-attempt middleware, with request signing as the example: it runs once per send, inside the retry layer.

The signer below is the shape the ``Middleware`` protocol exists for: an HMAC-SHA256 over the method, the path, a
timestamp, a nonce and the body. An ``httpx2.Auth`` flow runs once per call, so a retried request would carry the first
attempt's nonce, which a server that rejects replays refuses; a middleware signs every attempt afresh.
"""

from __future__ import annotations

import hashlib
import hmac
import itertools
from typing import TYPE_CHECKING, ClassVar, cast, override

import httpx2
import pytest

from headgate import HttpOptions, Rate, TransportPolicy, nothing
from headgate._core import CallContext
from headgate.retry import RetryPolicy
from tests.security._web import BASE, Web
from tests.support.clients import StubClient
from tests.support.wire import Script

if TYPE_CHECKING:
    from headgate import Middleware, Next
    from headgate.events import Event

_SECRET = b"shared-secret"
_ATTEMPTS = 3


class HmacSigner:
    """Sign each attempt: ``X-Signature`` over method, path, timestamp, nonce and body, with a fresh nonce each time."""

    credential_headers: ClassVar = frozenset({"X-Nonce", "X-Timestamp", "X-Signature"})
    credential_params: ClassVar = frozenset[str]()

    def __init__(self, secret: bytes) -> None:
        """Sign with *secret*; nonces and timestamps count up, so each attempt's are new."""
        self._secret = secret
        self._nonces = itertools.count(1)

    async def __call__(self, request: httpx2.Request, call_next: Next) -> httpx2.Response:
        """Stamp the nonce, timestamp and signature, then send."""
        nonce = str(next(self._nonces))
        request.headers["X-Nonce"] = nonce
        request.headers["X-Timestamp"] = f"1700000000{nonce}"
        request.headers["X-Signature"] = signature(self._secret, request)
        return await call_next(request)


def signature(secret: bytes, request: httpx2.Request) -> str:
    """Return the HMAC-SHA256 the signer stamps on *request*, as the server would recompute it."""
    message = b"\n".join(
        (
            request.method.encode(),
            request.url.raw_path,
            request.headers["X-Timestamp"].encode(),
            request.headers["X-Nonce"].encode(),
            request.content,
        )
    )
    return hmac.new(secret, message, hashlib.sha256).hexdigest()


def _client(script: Script, *middleware: HmacSigner | Recorder | ShortCircuit) -> StubClient:
    """Build a client over *script* with *middleware*, retrying up to three sends."""
    return StubClient(
        base_url="https://api.example.test",
        rate=Rate.per_second(1000),
        policy=TransportPolicy(retry=RetryPolicy(attempts=_ATTEMPTS)),
        http=HttpOptions(transport=script.transport(), middleware=middleware),
    )


class _VerifyingServer(Script):
    """A scripted network that checks each request's signature as it arrives, as the real server would."""

    def __init__(self, *responses: int) -> None:
        """Serve *responses* in order."""
        super().__init__(*responses)
        self.received: list[tuple[str, bool]] = []

    @override
    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        """Record the nonce and whether the signature verifies, then answer."""
        valid = hmac.compare_digest(request.headers["X-Signature"], signature(_SECRET, request))
        self.received.append((request.headers["X-Nonce"], valid))
        return super().__call__(request)


async def test_every_attempt_is_signed_afresh() -> None:
    """Sign each retry with a new nonce and a signature that verifies, not a replay of the first.

    The POST carries an idempotency key, which is what makes re-sending it safe.
    """
    server = _VerifyingServer(503, 503, 200)
    client = _client(server, HmacSigner(_SECRET))
    await client.post("orders", expect=nothing, json={"qty": 1}, idempotency_key="order-1")

    assert server.received == [("1", True), ("2", True), ("3", True)]


class Recorder:
    """A middleware that records the order it ran in and the attempt it saw."""

    credential_headers: ClassVar = frozenset[str]()
    credential_params: ClassVar = frozenset[str]()

    def __init__(self, name: str, log: list[tuple[str, int]]) -> None:
        """Record under *name* into *log*."""
        self._name = name
        self._log = log

    async def __call__(self, request: httpx2.Request, call_next: Next) -> httpx2.Response:
        """Record, then send."""
        context = CallContext.find(request)
        self._log.append((self._name, 0 if context is None else context.attempt))
        return await call_next(request)


async def test_middleware_runs_in_order_once_per_attempt() -> None:
    """Run the middleware in the order given, the first outermost, once for each send."""
    log: list[tuple[str, int]] = []
    await _client(Script(503, 200), Recorder("outer", log), Recorder("inner", log)).get("x", expect=nothing)

    assert log == [("outer", 1), ("inner", 1), ("outer", 2), ("inner", 2)]


class ShortCircuit:
    """A middleware that answers without sending."""

    credential_headers: ClassVar = frozenset[str]()
    credential_params: ClassVar = frozenset[str]()

    async def __call__(self, request: httpx2.Request, call_next: Next) -> httpx2.Response:
        """Answer 204 without calling the rest of the chain."""
        del call_next
        return httpx2.Response(204, request=request)


async def test_a_middleware_can_answer_without_sending() -> None:
    """Return a middleware's own response, and send nothing to the network."""
    script = Script()
    await _client(script, ShortCircuit()).get("x", expect=nothing)

    assert script.sent == []


class _Silent:
    """An observer that ignores every event."""

    def notify(self, event: Event) -> None:
        """Ignore *event*."""


@pytest.mark.parametrize("count", [0, 1])
async def test_the_client_closes_the_network_under_the_middleware(count: int) -> None:
    """Close the network transport on ``aclose``, with or without middleware (and the attempt layer) above it."""
    closed: list[bool] = []

    class _Network(httpx2.MockTransport):
        """Records its own close."""

        @override
        async def aclose(self) -> None:
            closed.append(True)

    script = Script()
    middleware = (Recorder("r", []),) * count
    client = StubClient(
        base_url="https://api.example.test",
        rate=Rate.per_second(1000),
        http=HttpOptions(transport=_Network(script), middleware=middleware, observers=(_Silent(),) * count),
    )
    await client.aclose()

    assert closed == [True]


class _Bare:
    """A middleware that declares nothing."""

    async def __call__(self, request: httpx2.Request, call_next: Next) -> httpx2.Response:
        """Send."""
        return await call_next(request)


class _Typo(_Bare):
    """A middleware that misspells ``credential_headers``."""

    credential_header: ClassVar = frozenset({"x-sig"})
    credential_params: ClassVar = frozenset[str]()


class _HeadersOnly(_Bare):
    """A middleware that declares only its headers."""

    credential_headers: ClassVar = frozenset({"x-sig"})


class _HeadersAsSet(_Bare):
    """A middleware that declares its headers as a mutable ``set``."""

    credential_headers: ClassVar[set[str]] = {"x-sig"}
    credential_params: ClassVar = frozenset[str]()


async def _bare_function(request: httpx2.Request, call_next: Next) -> httpx2.Response:
    """Stand in for a plain function used as middleware."""
    return await call_next(request)


@pytest.mark.parametrize(
    ("item", "name"),
    [
        pytest.param(_Bare(), "_Bare", id="nothing"),
        pytest.param(_Typo(), "_Typo", id="typo"),
        pytest.param(_HeadersOnly(), "_HeadersOnly", id="headers-only"),
        pytest.param(_HeadersAsSet(), "_HeadersAsSet", id="set"),
        pytest.param(_bare_function, "_bare_function", id="function"),
    ],
)
def test_a_middleware_that_declares_nothing_is_refused(item: object, name: str) -> None:
    """Refuse a middleware that does not declare both names as frozensets, and name it."""
    with pytest.raises(TypeError, match="must declare credential_headers and credential_params") as raised:
        HttpOptions(middleware=(cast("Middleware", item),))

    assert f"middleware {name} must" in str(raised.value)


class _PropertySigner:
    """A signing middleware that declares what it stamps through properties."""

    @property
    def credential_headers(self) -> frozenset[str]:
        """Return the header it stamps."""
        return frozenset({"X-Signature"})

    @property
    def credential_params(self) -> frozenset[str]:
        """Return no parameters."""
        return frozenset[str]()

    async def __call__(self, request: httpx2.Request, call_next: Next) -> httpx2.Response:
        """Stamp the signature, then send."""
        request.headers["X-Signature"] = "signed"
        return await call_next(request)


async def test_a_middleware_may_declare_through_properties() -> None:
    """Accept properties as the declaration, and withhold the header they name on a cross-origin hop."""
    web = Web({f"{BASE}/start": "https://files.example/signed"})
    async with web.client(http=HttpOptions(middleware=(_PropertySigner(),))) as client:
        await client.get("start", expect=nothing)

    first, hop = web.sent
    assert "X-Signature" in first.headers
    assert "X-Signature" not in hop.headers
