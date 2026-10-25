# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""Every kind of ``ClientError`` a call can raise, pinned whole: its message, its type and its public attributes.

One scenario per failure kind drives a real client over a scripted network and snapshots what the caller sees. The
message is the contract a boundary's error detail and an operator's log line rely on, so a change to any word of it, to
the type, or to an attribute (``retryable``, ``retry_after``, ``attempts``...) shows up here as a reviewed diff. Clients
run on a :class:`~headgate.testing.TestClock`, so every duration they measure (a note's, a deadline's elapsed time)
reads ``0s`` and nothing in a snapshot depends on how fast the suite runs.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Final, override

import httpx2
import pytest

from headgate import (
    AuthError,
    Backoff,
    BreakerPolicy,
    BreakerRegistry,
    CachedTokenAuth,
    ClientError,
    HttpOptions,
    Problem,
    RateLimitError,
    RefusedError,
    RetryPolicy,
    TransportPolicy,
    json_reader,
    nothing,
)
from headgate.ratelimit import UNLIMITED
from headgate.retry import NO_RETRY
from headgate.testing import TestClock
from tests.support.clients import StubClient
from tests.support.wire import Script

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterator

    from syrupy.assertion import SnapshotAssertion

    from headgate._responses import Reader, RefusalCheck

_BASE: Final = "https://api.example.test/v1"
#: A credential in the query string, which every message must redact.
_KEYED_QUERY: Final = "?api_key=sk-live-SECRET"
#: A breaker's cooldown, and the ``Retry-After`` the rate-limited scenarios send.
_COOLDOWN: Final = 30.0
_RETRY_AFTER: Final = "7"
#: The response cap the too-large scenario sets, below its body.
_CAP: Final = 16
#: The deadline the hanging scenario sets: short, so the suite does not wait.
_DEADLINE: Final = 0.02

#: The public attributes a ``ClientError`` may carry, in the order they are rendered.
_ATTRIBUTES: Final = (
    "source",
    "method",
    "url",
    "status_code",
    "code",
    "content_type",
    "request_id",
    "attempts",
    "retryable",
    "retry_after",
)

type Scenario = Callable[[], Awaitable[None]]


@pytest.fixture(autouse=True)
def test_clock() -> Iterator[TestClock]:
    """Build every client in the test on a manual clock, so the durations its notes name are exactly ``0s``."""
    with TestClock().install() as clock:
        yield clock


def _client(
    *responses: httpx2.Response | int,
    policy: TransportPolicy | None = None,
    http: HttpOptions | None = None,
    auth: httpx2.Auth | None = None,
) -> StubClient:
    """Return a client over a script serving *responses*, sending each call once unless *policy* says otherwise."""
    transport = Script(*responses).transport()
    options = http or HttpOptions()
    return StubClient(
        base_url=_BASE,
        rate=UNLIMITED,
        auth=auth,
        policy=policy or TransportPolicy(retry=NO_RETRY, breaker=None),
        http=HttpOptions(
            transport=options.transport or transport,
            max_response_bytes=options.max_response_bytes,
            refusal_check=options.refusal_check,
            follow_redirects=options.follow_redirects,
            allow_private_hosts=options.allow_private_hosts,
            max_redirects=options.max_redirects,
        ),
    )


def _fail(exc_type: type[httpx2.TransportError], message: str) -> httpx2.MockTransport:
    """Return a network that raises *exc_type* with *message* for every request."""

    def handler(request: httpx2.Request) -> httpx2.Response:
        """Fail the request."""
        raise exc_type(message, request=request)

    return httpx2.MockTransport(handler)


async def _get(client: StubClient, *, expect: Reader[object] = nothing) -> None:
    """Send one GET for an order through *client*, with a credential in its query, closing the client afterwards."""
    async with client:
        await client.get("orders" + _KEYED_QUERY, expect=expect)


# --- The scenarios, one per failure kind ---


async def _transport_error() -> None:
    """Fail with a refused connection: ``TransportError``."""
    await _get(_client(http=HttpOptions(transport=_fail(httpx2.ConnectError, "connection refused"))))


async def _transport_error_without_message() -> None:
    """Fail with a dropped connection whose error has no message: the type name stands in."""
    await _get(_client(http=HttpOptions(transport=_fail(httpx2.RemoteProtocolError, ""))))


async def _timeout() -> None:
    """Fail with a read timeout: ``ClientTimeoutError``."""
    await _get(_client(http=HttpOptions(transport=_fail(httpx2.ReadTimeout, "timed out"))))


async def _undeclared_4xx() -> None:
    """Answer an undeclared 404 with a body and a request id."""
    await _get(_client(httpx2.Response(404, text="no such order", headers={"x-request-id": "req-404"})))


async def _unauthorized() -> None:
    """Answer 401: ``AuthError``."""
    await _get(_client(httpx2.Response(401, text="bad key")))


async def _forbidden() -> None:
    """Answer 403: ``AuthError``."""
    await _get(_client(httpx2.Response(403)))


async def _surviving_429() -> None:
    """Answer 429 with ``Retry-After`` on every attempt: ``RateLimitError`` carrying the delay."""
    await _get(_client(httpx2.Response(429, headers={"retry-after": _RETRY_AFTER}, text="slow down")))


async def _surviving_503() -> None:
    """Answer 503 on every attempt of three, under a policy with a fixed backoff: the history is in the notes."""
    policy = TransportPolicy(
        retry=RetryPolicy(attempts=3, backoff=Backoff(jitter=lambda: 0.0), budget=None), breaker=None
    )
    await _get(_client(httpx2.Response(503, text="maintenance"), policy=policy))


async def _surviving_429_past_max_retry_after() -> None:
    """Answer 429 asking for longer than ``max_retry_after``: the retry layer stops and the note says why."""
    policy = TransportPolicy(retry=RetryPolicy(attempts=3, max_retry_after=1.0, budget=None), breaker=None)
    await _get(_client(httpx2.Response(429, headers={"retry-after": _RETRY_AFTER}), policy=policy))


async def _surviving_503_on_a_post() -> None:
    """Answer 503 to a POST without an idempotency key: transient, but not retryable."""
    async with _client(503) as client:
        await client.post("orders", expect=nothing, json={"sku": "a"})


async def _surviving_501() -> None:
    """Answer 501, a status the policy does not retry: not called transient."""
    await _get(_client(httpx2.Response(501, text="not implemented")))


async def _surviving_505() -> None:
    """Answer 505, a status the policy does not retry: not called transient."""
    await _get(_client(505))


def _refuse_quota(_resp: httpx2.Response) -> bytes:
    """Refuse a 200 body as a provider's quota error."""
    msg = "quota exhausted"
    raise RefusedError(msg, code="QUOTA", kind=RateLimitError)


async def _reader_refusal() -> None:
    """Refuse inside a 200 body, from a reader: the reader's kind, code and the wire's status."""
    await _get(_client(httpx2.Response(200, text='{"error":"quota"}')), expect=_refuse_quota)


def _sign_in_redirect(resp: httpx2.Response) -> RefusedError | None:
    """Name a redirect to a sign-in page as a refused credential."""
    if resp.has_redirect_location:
        return RefusedError("key rejected", code="KEY", kind=AuthError)
    return None


async def _host_check_refusal() -> None:
    """Refuse a key-error redirect from the host's ``refusal_check``: the 302 it arrived with."""
    check: RefusalCheck = _sign_in_redirect
    redirect = httpx2.Response(302, headers={"location": "/login"})
    await _get(_client(redirect, http=HttpOptions(refusal_check=check, follow_redirects=False)))


async def _parse_error() -> None:
    """Answer a 2xx the reader cannot parse: ``ResponseParseError`` naming the status and the content type."""
    page = httpx2.Response(200, text="<html>", headers={"content-type": "text/html"})
    await _get(_client(page), expect=json_reader(json.loads))


async def _too_large() -> None:
    """Answer a body past ``max_response_bytes``: ``ResponseTooLargeError``."""
    await _get(_client(httpx2.Response(200, content=b"x" * (_CAP * 2)), http=HttpOptions(max_response_bytes=_CAP)))


async def _problem_json() -> None:
    """Answer an RFC 9457 problem detail: its title and detail replace the body in the message."""
    document = (
        b'{"type":"https://example.test/out-of-stock","title":"Out of stock","detail":"sku a","status":409,"sku":"a"}'
    )
    await _get(_client(httpx2.Response(409, content=document, headers={"content-type": "application/problem+json"})))


class _Hanging(httpx2.AsyncBaseTransport):
    """A network that never answers."""

    @override
    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        await asyncio.Event().wait()
        return httpx2.Response(200)


async def _deadline_exceeded() -> None:
    """Hang past the call's deadline: ``ClientTimeoutError`` naming the request line."""
    async with StubClient(
        base_url=_BASE,
        rate=UNLIMITED,
        policy=TransportPolicy(retry=NO_RETRY, breaker=None),
        http=HttpOptions(transport=_Hanging()),
    ) as client:
        await client.get("orders" + _KEYED_QUERY, expect=nothing, deadline=_DEADLINE)


async def _open_breaker() -> None:
    """Call an origin whose breaker a 503 opened: ``CircuitOpenError`` with the whole cooldown left."""
    registry = BreakerRegistry(clock=lambda: 0.0)
    policy = TransportPolicy(
        retry=NO_RETRY, breaker=BreakerPolicy(failure_threshold=1, recovery_timeout=_COOLDOWN, registry=registry)
    )
    async with _client(503, policy=policy) as client:
        with pytest.raises(ClientError):
            await client.get("orders", expect=nothing)
        await client.get("orders" + _KEYED_QUERY, expect=nothing)


async def _forbidden_destination() -> None:
    """Call a loopback address with the SSRF guard on: ``ForbiddenDestinationError`` before anything is sent."""
    async with StubClient(
        base_url="https://127.0.0.1:8443",
        rate=UNLIMITED,
        policy=TransportPolicy(retry=NO_RETRY, breaker=None),
        http=HttpOptions(transport=Script().transport(), allow_private_hosts=False),
    ) as client:
        await client.get("admin" + _KEYED_QUERY, expect=nothing)


class _MintRejected(CachedTokenAuth):
    """Mint on a client of its own against a token endpoint that answers 401."""

    @override
    async def _mint_token(self) -> tuple[str, float]:
        transport = httpx2.MockTransport(lambda _request: httpx2.Response(401, text="invalid_client"))
        async with httpx2.AsyncClient(transport=transport) as client:
            response = await client.post("https://auth.example.test/token?client_secret=S3CR3T")
        response.raise_for_status()
        return "never", 60.0


async def _mint_failure() -> None:
    """Fail the token mint with a 401: ``AuthError`` naming the token endpoint, its secret redacted."""
    await _get(_client(200, auth=_MintRejected()))


class _MintUnreadable(CachedTokenAuth):
    """Mint from a token response with no ``access_token``."""

    @override
    async def _mint_token(self) -> tuple[str, float]:
        return {"token": "x"}["access_token"], 60.0


async def _mint_unreadable() -> None:
    """Fail the token mint on an unreadable response: ``ResponseParseError``."""
    await _get(_client(200, auth=_MintUnreadable()))


class _MintUnreachable(CachedTokenAuth):
    """Mint against a token host that refuses the connection."""

    @override
    async def _mint_token(self) -> tuple[str, float]:
        request = httpx2.Request("POST", "https://auth.example.test/token?client_secret=S3CR3T")
        msg = "connection refused"
        raise httpx2.ConnectError(msg, request=request)


async def _mint_unreachable() -> None:
    """Fail the token mint on a refused connection: ``TransportError`` naming the token endpoint."""
    await _get(_client(200, auth=_MintUnreachable()))


async def _redirect_loop() -> None:
    """Follow a redirect loop past ``max_redirects``: a ``TransportError`` that is not retryable."""
    loop = httpx2.Response(302, headers={"location": f"{_BASE}/orders"})
    await _get(_client(loop, http=HttpOptions(max_redirects=2)))


_SCENARIOS: Final[dict[str, Scenario]] = {
    "transport_error": _transport_error,
    "transport_error_without_message": _transport_error_without_message,
    "timeout": _timeout,
    "undeclared_4xx": _undeclared_4xx,
    "auth_401": _unauthorized,
    "auth_403": _forbidden,
    "surviving_429_retry_after": _surviving_429,
    "surviving_429_past_max_retry_after": _surviving_429_past_max_retry_after,
    "surviving_503_with_history": _surviving_503,
    "surviving_503_on_a_post": _surviving_503_on_a_post,
    "surviving_501": _surviving_501,
    "surviving_505": _surviving_505,
    "reader_refusal": _reader_refusal,
    "host_check_refusal": _host_check_refusal,
    "parse_error": _parse_error,
    "too_large": _too_large,
    "problem_json": _problem_json,
    "deadline_exceeded": _deadline_exceeded,
    "open_breaker": _open_breaker,
    "forbidden_destination": _forbidden_destination,
    "mint_failure": _mint_failure,
    "mint_unreadable": _mint_unreadable,
    "mint_unreachable": _mint_unreachable,
    "redirect_loop": _redirect_loop,
}


def _problem(problem: Problem | None) -> dict[str, object] | None:
    """Return *problem*'s members as plain values, or ``None``."""
    if problem is None:
        return None
    return {
        "type": problem.type,
        "title": problem.title,
        "status": problem.status,
        "detail": problem.detail,
        "instance": problem.instance,
        "extensions": dict(problem.extensions),
    }


def _render(error: ClientError) -> dict[str, object]:
    """Return what a caller sees of *error*: its type, message, public attributes and notes, unstable parts masked."""
    rendered: dict[str, object] = {
        "type": type(error).__name__,
        "message": str(error),
    }
    rendered.update({name: getattr(error, name) for name in _ATTRIBUTES if hasattr(error, name)})
    rendered["problem"] = _problem(getattr(error, "problem", None))
    rendered["notes"] = list(getattr(error, "__notes__", ()))
    return rendered


@pytest.mark.parametrize("scenario", list(_SCENARIOS))
async def test_the_error_a_failure_raises_matches_the_snapshot(scenario: str, snapshot: SnapshotAssertion) -> None:
    """Raise exactly the reviewed error, message and attributes for each kind of failure."""
    with pytest.raises(ClientError) as raised:
        await _SCENARIOS[scenario]()

    assert _render(raised.value) == snapshot
