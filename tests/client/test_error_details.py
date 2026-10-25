# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""What a ``ClientError`` records about its call, and RFC 9457 problem details on a status error.

The attributes are read-only and survive pickling exactly, like the message. A problem detail is parsed only from a
body declared ``application/problem+json`` and bounded in size, and its title and detail replace the raw body in the
message.
"""

from __future__ import annotations

import copy
import json
import pickle
from types import MappingProxyType
from typing import TYPE_CHECKING, override

import httpx2
import pytest

from headgate import (
    BreakerPolicy,
    BreakerRegistry,
    CircuitOpenError,
    ClientError,
    HttpOptions,
    Problem,
    Rate,
    RateLimitError,
    RefusedError,
    ResponseParseError,
    ResponseTooLargeError,
    RetryPolicy,
    TransportError,
    TransportPolicy,
    UpstreamStatusError,
    json_reader,
    nothing,
)
from headgate._core import CallContext
from headgate.retry import NO_RETRY
from tests.support.breakers import failures
from tests.support.clients import StubClient
from tests.support.wire import Script

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

    from headgate._middleware import Next

_BASE = "https://api.example.test"
_REQUEST_ID = "req-123"
_RETRY_AFTER = 2.0
_PROBLEM = "application/problem+json"
_LATIN_1 = "text/plain; charset=latin-1"
#: The longest request id an error keeps.
_REQUEST_ID_LIMIT = 200


def _client(script: Script | httpx2.MockTransport) -> StubClient:
    """Build a client over *script* that sends each call once."""
    transport = script if isinstance(script, httpx2.MockTransport) else script.transport()
    return StubClient(
        base_url=_BASE,
        rate=Rate.per_second(1000),
        policy=TransportPolicy(retry=NO_RETRY),
        http=HttpOptions(transport=transport),
    )


async def _raised(client: StubClient) -> ClientError:
    """Return what one GET through *client* raises."""
    with pytest.raises(ClientError) as raised:
        await client.get("orders", expect=nothing)
    return raised.value


async def test_a_status_error_records_its_call() -> None:
    """Record the method, URL, request id, attempt count, and that sending again would not help."""
    error = await _raised(_client(Script(httpx2.Response(404, headers={"X-Request-Id": _REQUEST_ID}))))

    assert (error.method, error.url, error.request_id, error.attempts) == ("GET", "orders", _REQUEST_ID, 1)
    assert error.retryable is False
    assert error.retry_after is None


@pytest.mark.parametrize(("status", "kind"), [(503, UpstreamStatusError), (429, RateLimitError)])
async def test_a_surviving_transient_status_is_retryable_with_its_retry_after(
    status: int, kind: type[UpstreamStatusError]
) -> None:
    """Mark a surviving 429 or 503 retryable and carry the ``Retry-After`` it asked for."""
    error = await _raised(_client(Script(httpx2.Response(status, headers={"Retry-After": "2"}))))

    assert isinstance(error, kind)
    assert error.retryable is True
    assert error.retry_after == _RETRY_AFTER


async def test_only_a_429_or_503_carries_retry_after() -> None:
    """Ignore ``Retry-After`` on a 500, which RFC 9110 does not give it a meaning on."""
    error = await _raised(_client(Script(httpx2.Response(500, headers={"Retry-After": "2"}))))
    assert error.retryable is True
    assert error.retry_after is None


async def test_the_request_id_is_read_from_the_configured_headers_and_bounded() -> None:
    """Read the first configured header the response carries, cut to a bounded length."""
    long_id = "x" * (_REQUEST_ID_LIMIT * 2)
    script = Script(httpx2.Response(404, headers={"X-Trace": long_id, "X-Request-Id": _REQUEST_ID}))
    client = StubClient(
        base_url=_BASE,
        rate=Rate.per_second(1000),
        http=HttpOptions(transport=script.transport(), request_id_headers=("x-trace", "x-request-id")),
    )

    error = await _raised(client)

    assert error.request_id is not None
    assert len(error.request_id) == _REQUEST_ID_LIMIT
    assert error.request_id.startswith("xxx")


def _refuse(request: httpx2.Request) -> httpx2.Response:
    """Fail the connection."""
    msg = "connection refused"
    raise httpx2.ConnectError(msg, request=request)


def _unencodable(request: httpx2.Request) -> httpx2.Response:
    """Refuse to encode the request, as h11 does for an illegal header value."""
    msg = "Illegal header value"
    raise httpx2.LocalProtocolError(msg, request=request)


async def test_a_network_failure_is_retryable() -> None:
    """Mark a connection failure retryable: the request may succeed on another attempt."""
    error = await _raised(_client(httpx2.MockTransport(_refuse)))
    assert isinstance(error, TransportError)
    assert error.retryable is True
    assert error.request_id is None


async def test_a_request_the_library_could_not_encode_is_not_retryable() -> None:
    """Mark a request httpx2 refused to encode not retryable: it fails the same way every time."""
    error = await _raised(_client(httpx2.MockTransport(_unencodable)))
    assert isinstance(error, TransportError)
    assert error.retryable is False


@pytest.mark.parametrize("status", [501, 505, 507])
async def test_a_surviving_status_the_policy_does_not_retry_is_not_retryable(status: int) -> None:
    """Mark a 5xx outside ``RetryPolicy.statuses`` not retryable, and still charge it to the origin's breaker."""
    error = await _raised(_client(Script(status)))

    assert isinstance(error, UpstreamStatusError)
    assert (error.status_code, error.retryable) == (status, False)
    assert failures("api.example.test") == 1


@pytest.mark.parametrize("status", [501, 505])
async def test_a_surviving_status_the_policy_does_not_retry_is_not_called_transient(status: int) -> None:
    """Word a 5xx outside ``RetryPolicy.statuses`` as the upstream's status, keeping "transient" for what is retried."""
    error = await _raised(_client(Script(status)))

    assert str(error).startswith(f"[StubClient] upstream {status} for GET orders")


async def test_a_status_the_server_says_to_retry_is_called_transient() -> None:
    """Word a 501 the server marks ``x-should-retry: true`` as transient: the policy counts it as a failure to retry."""
    error = await _raised(_client(Script(httpx2.Response(501, headers={"x-should-retry": "true"}))))

    assert str(error).startswith("[StubClient] transient 501 for GET orders")
    assert error.retryable is True


#: The redirect hops a loop is followed for before it is refused.
_LOOP_HOPS = 2


async def test_a_redirect_loop_is_not_retried_nor_charged_to_the_breaker() -> None:
    """Refuse a redirect loop once, not retryable and not charged: the origin answered every hop, as it would again."""
    script = Script(httpx2.Response(302, headers={"location": f"{_BASE}/orders"}))
    breaker = BreakerPolicy(failure_threshold=1, registry=BreakerRegistry())
    client = StubClient(
        base_url=_BASE,
        rate=Rate.per_second(1000),
        policy=TransportPolicy(breaker=breaker),
        http=HttpOptions(transport=script.transport(), max_redirects=_LOOP_HOPS),
    )

    for _ in range(2):
        error = await _raised(client)
        assert type(error) is TransportError
        assert (error.retryable, error.attempts) == (False, _LOOP_HOPS + 1)

    assert len(script.sent) == 2 * (_LOOP_HOPS + 1), "each call followed the chain once; neither was re-sent"


async def test_a_post_without_a_key_that_survived_a_503_is_not_retryable() -> None:
    """Mark a surviving 503 on a POST without an idempotency key not retryable: sending it again is not safe."""
    with pytest.raises(UpstreamStatusError) as raised:
        await _client(Script(503)).post("orders", expect=nothing, json={"sku": "a"})

    assert raised.value.retryable is False


async def test_a_503_the_server_says_not_to_retry_is_not_retryable() -> None:
    """Follow the server's ``x-should-retry: false``, as the retry policy does."""
    error = await _raised(_client(Script(httpx2.Response(503, headers={"x-should-retry": "false"}))))

    assert error.retryable is False


class _BrokenBody(httpx2.AsyncByteStream):
    """A response body the connection drops after its first chunk."""

    @override
    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield b"{"
        msg = "connection dropped mid-body"
        raise httpx2.ReadError(msg)


def _dropped_mid_body(_request: httpx2.Request) -> httpx2.Response:
    """Answer 200 with a body whose connection drops."""
    return httpx2.Response(200, stream=_BrokenBody())


async def _get(client: StubClient) -> None:
    """Make a GET."""
    await client.get("orders", expect=nothing)


async def _put(client: StubClient) -> None:
    """Make a PUT with a JSON body."""
    await client.put("orders/1", expect=nothing, json={"sku": "a"})


async def _post(client: StubClient) -> None:
    """Make a POST with a JSON body and no idempotency key."""
    await client.post("orders", expect=nothing, json={"sku": "a"})


@pytest.mark.parametrize(
    ("call", "retryable"),
    [pytest.param(_get, True, id="GET"), pytest.param(_put, True, id="PUT"), pytest.param(_post, False, id="POST")],
)
async def test_a_body_read_failure_is_retryable_only_for_a_method_safe_to_resend(
    call: Callable[[StubClient], Awaitable[None]], *, retryable: bool
) -> None:
    """Judge a failure reading the body as the retry policy judges a network failure: by the method's safety."""
    with pytest.raises(TransportError) as raised:
        await call(_client(httpx2.MockTransport(_dropped_mid_body)))

    assert raised.value.retryable is retryable


async def test_an_open_breaker_raises_circuit_open_error_with_its_cooldown() -> None:
    """Refuse without sending, as ``CircuitOpenError`` (a ``TransportError``) carrying the cooldown left."""
    registry = BreakerRegistry()
    breaker = BreakerPolicy(failure_threshold=1, recovery_timeout=3600.0, registry=registry)
    script = Script(503)
    client = StubClient(
        base_url=_BASE,
        rate=Rate.per_second(1000),
        policy=TransportPolicy(retry=NO_RETRY, breaker=breaker),
        http=HttpOptions(transport=script.transport()),
    )
    await _raised(client)

    error = await _raised(client)

    assert isinstance(error, CircuitOpenError)
    assert isinstance(error, TransportError)
    assert error.attempts == 0, "nothing was sent"
    assert error.retry_after is not None
    assert 0 < error.retry_after <= breaker.recovery_timeout
    assert len(script.sent) == 1


async def test_a_parse_error_names_the_status_and_content_type() -> None:
    """Record the status and declared content type of a 2xx body its reader could not read."""
    script = Script(httpx2.Response(200, content=b"<html>", headers={"Content-Type": "text/html"}))
    with pytest.raises(ResponseParseError) as raised:
        await _client(script).get("orders", expect=json_reader(json.loads))

    assert (raised.value.status_code, raised.value.content_type) == (200, "text/html")


async def test_notes_a_layer_added_reach_the_error() -> None:
    """Add every note the call's layers recorded to the ``ClientError`` it raises."""

    async def annotate(request: httpx2.Request, call_next: Next) -> httpx2.Response:
        context = CallContext.find(request)
        assert context is not None
        context.add_note(f"attempt {context.attempt}: sent")
        return await call_next(request)

    client = StubClient(
        base_url=_BASE,
        rate=Rate.per_second(1000),
        policy=TransportPolicy(retry=NO_RETRY),
        http=HttpOptions(transport=Script(404).transport(), middleware=(annotate,)),
    )
    error = await _raised(client)

    assert error.__notes__[0] == "attempt 1: sent", "the layer's note comes first, then the retry layer's history"


async def test_a_body_over_the_cap_records_its_call() -> None:
    """Record the call on ``ResponseTooLargeError`` like on every other ``ClientError``."""
    cap = 4
    script = Script(httpx2.Response(200, content=b"x" * (cap * 2), headers={"X-Request-Id": _REQUEST_ID}))
    client = StubClient(
        base_url=_BASE,
        rate=Rate.per_second(1000),
        http=HttpOptions(transport=script.transport(), max_response_bytes=cap),
    )

    error = await _raised(client)

    assert isinstance(error, ResponseTooLargeError)
    assert (error.method, error.url, error.request_id, error.attempts) == ("GET", "orders", _REQUEST_ID, 1)


# --- Problem details ---


def _problem_response(document: object, *, status: int = 400, media_type: str = _PROBLEM) -> Script:
    """Return a network answering *status* with *document* as a JSON body of *media_type*."""
    body = json.dumps(document).encode()
    return Script(httpx2.Response(status, content=body, headers={"Content-Type": media_type}))


async def test_a_problem_detail_is_parsed_and_named_in_the_message() -> None:
    """Parse the standard members and the extensions, and name the title and detail instead of the raw body."""
    document = {
        "type": "https://example.test/probs/out-of-credit",
        "title": "You do not have enough credit.",
        "status": 403,
        "detail": "Your current balance is 30, but that costs 50.",
        "instance": "/account/12345/msgs/abc",
        "balance": 30,
        "accounts": ["/account/12345", {"id": 67890}],
    }
    error = await _raised(_client(_problem_response(document, media_type=f"{_PROBLEM}; charset=utf-8")))

    assert isinstance(error, UpstreamStatusError)
    assert error.problem == Problem(
        type="https://example.test/probs/out-of-credit",
        title="You do not have enough credit.",
        status=403,
        detail="Your current balance is 30, but that costs 50.",
        instance="/account/12345/msgs/abc",
        extensions={"balance": 30, "accounts": ["/account/12345", {"id": 67890}]},
    )
    assert str(error).endswith("— You do not have enough credit.: Your current balance is 30, but that costs 50.")
    assert "accounts" not in str(error), "the raw body is not repeated"


async def test_a_problems_extensions_are_read_only_at_every_depth() -> None:
    """Freeze the extensions into read-only mappings and tuples."""
    error = await _raised(_client(_problem_response({"title": "t", "errors": [{"field": "qty"}]})))

    assert isinstance(error, UpstreamStatusError)
    assert error.problem is not None
    extensions = error.problem.extensions
    assert isinstance(extensions, MappingProxyType)
    errors = extensions["errors"]
    assert isinstance(errors, tuple)
    assert isinstance(errors[0], MappingProxyType)


async def test_a_member_of_the_wrong_type_is_ignored_and_type_defaults() -> None:
    """Ignore a standard member of the wrong JSON type, as RFC 9457 requires, and default ``type``."""
    error = await _raised(_client(_problem_response({"type": 7, "title": ["x"], "status": True, "detail": "d"})))

    assert isinstance(error, UpstreamStatusError)
    assert error.problem == Problem(detail="d")
    assert str(error).endswith("— d")


async def test_a_problem_without_title_or_detail_keeps_the_body_snippet() -> None:
    """Fall back to the body snippet when the problem names neither a title nor a detail."""
    error = await _raised(_client(_problem_response({"title": "   ", "code": "E42"})))

    assert isinstance(error, UpstreamStatusError)
    assert error.problem is not None
    assert '"code": "E42"' in str(error)


@pytest.mark.parametrize(
    "script",
    [
        pytest.param(_problem_response({"title": "t"}, media_type="application/json"), id="not-declared-a-problem"),
        pytest.param(_problem_response(["not", "an", "object"]), id="not-an-object"),
        pytest.param(Script(httpx2.Response(400, content=b"{", headers={"Content-Type": _PROBLEM})), id="not-json"),
        pytest.param(
            Script(httpx2.Response(400, content=b"[" * 100_000, headers={"Content-Type": _PROBLEM})), id="too-large"
        ),
        pytest.param(
            Script(httpx2.Response(400, content=b"[" * 30_000 + b"]" * 30_000, headers={"Content-Type": _PROBLEM})),
            id="nested-too-deep",
        ),
    ],
)
async def test_a_body_that_is_not_a_problem_detail_has_none(script: Script) -> None:
    """Parse nothing from a body not declared a problem, not a JSON object, malformed, too large or too deep."""
    error = await _raised(_client(script))
    assert isinstance(error, UpstreamStatusError)
    assert error.problem is None


async def test_a_surviving_503_parses_its_problem_detail_too() -> None:
    """Parse a problem detail on a retried-out 503 as on any status error, and name its title in the message."""
    error = await _raised(_client(_problem_response({"title": "Down for maintenance"}, status=503)))
    assert isinstance(error, UpstreamStatusError)
    assert error.problem == Problem(title="Down for maintenance")
    assert str(error).endswith("(status=503) — Down for maintenance")


async def test_a_problem_with_no_title_member_is_read() -> None:
    """Read a problem that names only a detail, the title absent rather than null."""
    error = await _raised(_client(_problem_response({"detail": "sku a is out of stock"})))

    assert isinstance(error, UpstreamStatusError)
    assert error.problem == Problem(detail="sku a is out of stock")


#: The longest summary of a body a message carries, before the ``…`` that marks it cut.
_SUMMARY_LIMIT = 300


@pytest.mark.parametrize(("length", "expected_tail"), [(_SUMMARY_LIMIT, "x"), (_SUMMARY_LIMIT + 1, "…")])
async def test_a_problems_summary_is_cut_past_the_limit(length: int, expected_tail: str) -> None:
    """Keep a problem's title and detail whole up to the limit, and cut them to it with ``…`` past it."""
    error = await _raised(_client(_problem_response({"detail": "x" * length})))

    summary = str(error).rpartition(" — ")[2]
    assert summary.endswith(expected_tail)
    assert len(summary) == min(length, _SUMMARY_LIMIT + 1)


@pytest.mark.parametrize(("length", "expected_tail"), [(_SUMMARY_LIMIT, "x"), (_SUMMARY_LIMIT + 1, "…")])
async def test_a_body_snippet_is_cut_past_the_limit(length: int, expected_tail: str) -> None:
    """Keep a body whole up to the limit, and cut it to it with ``…`` past it."""
    error = await _raised(_client(Script(httpx2.Response(400, text="x" * length))))

    snippet = str(error).rpartition(" — ")[2]
    assert snippet.endswith(expected_tail)
    assert len(snippet) == min(length, _SUMMARY_LIMIT + 1)


async def test_a_body_snippet_is_decoded_in_the_charset_it_declares() -> None:
    """Decode the snippet in the response's declared charset, not UTF-8 regardless."""
    body = "café fermé".encode("latin-1")
    error = await _raised(_client(Script(httpx2.Response(400, content=body, headers={"Content-Type": _LATIN_1}))))

    assert str(error).endswith("— café fermé")


async def test_a_body_snippet_replaces_bytes_its_charset_cannot_decode() -> None:
    """Replace undecodable bytes in the snippet rather than fail to build the error."""
    error = await _raised(_client(Script(httpx2.Response(400, content=b"bad \xff byte"))))

    assert str(error).endswith("— bad \ufffd byte")


#: The largest problem document read.
_PROBLEM_SCAN_BYTES = 64 * 1024


async def test_a_problem_document_of_exactly_the_scan_limit_is_read() -> None:
    """Read a problem document of exactly the largest size parsed."""
    padding = _PROBLEM_SCAN_BYTES - len(json.dumps({"title": "t", "pad": ""}))
    body = json.dumps({"title": "t", "pad": " " * padding}).encode()
    error = await _raised(_client(Script(httpx2.Response(400, content=body, headers={"Content-Type": _PROBLEM}))))

    assert len(body) == _PROBLEM_SCAN_BYTES
    assert isinstance(error, UpstreamStatusError)
    assert error.problem is not None
    assert error.problem.title == "t"


async def test_a_surviving_status_outside_the_policy_still_carries_its_retry_after() -> None:
    """Report a 503's ``Retry-After`` though the call's policy does not retry 503s."""
    client = StubClient(
        base_url=_BASE,
        rate=Rate.per_second(1000),
        policy=TransportPolicy(retry=RetryPolicy(statuses=frozenset({500}))),
        http=HttpOptions(transport=Script(httpx2.Response(503, headers={"Retry-After": "2"})).transport()),
    )

    error = await _raised(client)

    assert (error.retryable, error.retry_after) == (False, _RETRY_AFTER)
    assert str(error).startswith("[StubClient] upstream 503")


async def test_a_surviving_500_is_transient_and_charged_to_the_breaker() -> None:
    """Raise a surviving 500 as a transient status, and charge it to the origin, as every 5xx is."""
    error = await _raised(_client(Script(500)))

    assert str(error).startswith("[StubClient] transient 500 for GET orders")
    assert failures("api.example.test") == 1


async def test_a_refusal_and_a_parse_error_carry_the_request_id() -> None:
    """Read the request id of the response a refusal or an unreadable body came on."""
    answer = httpx2.Response(200, text="<html>", headers={"x-request-id": _REQUEST_ID})

    def refuse(_resp: httpx2.Response) -> bytes:
        """Refuse the body."""
        msg = "refused"
        raise RefusedError(msg, code="R")

    async with _client(Script(answer)) as client:
        with pytest.raises(UpstreamStatusError) as refused:
            await client.get("orders", expect=refuse)
        with pytest.raises(ResponseParseError) as unreadable:
            await client.get("orders", expect=json_reader(json.loads))

    assert (refused.value.request_id, unreadable.value.request_id) == (_REQUEST_ID, _REQUEST_ID)


def _empty_value_error(_resp: httpx2.Response) -> bytes:
    """Fail as a reader whose error has no message."""
    raise ValueError


async def test_a_parse_error_without_a_message_names_the_error_type() -> None:
    """Name the reader's exception type when its message is empty."""
    async with _client(Script(200)) as client:
        with pytest.raises(ResponseParseError) as raised:
            await client.get("orders", expect=_empty_value_error)

    assert str(raised.value) == "[StubClient] unreadable 200 body for GET orders: ValueError"


# --- Pickling ---


async def test_an_error_with_details_and_a_problem_survives_a_reduce_round_trip() -> None:
    """Rebuild the message, the details, the problem and its frozen extensions as an unpickler does.

    ``copy.deepcopy`` replays the reduce protocol just as ``pickle.loads`` would, without deserializing bytes; that
    ``pickle.dumps`` succeeds shows every part is picklable by name.
    """
    error = await _raised(_client(_problem_response({"title": "t", "nested": {"a": [1, 2]}}, status=503)))

    restored = copy.deepcopy(error)

    assert type(restored) is type(error)
    assert str(restored) == str(error)
    assert restored.__dict__ == error.__dict__
    assert (restored.method, restored.url, restored.attempts, restored.retryable) == ("GET", "orders", 1, True)
    assert isinstance(restored, UpstreamStatusError)
    assert restored.problem is not None
    assert isinstance(restored.problem.extensions["nested"], MappingProxyType)
    assert pickle.dumps(error)


def test_a_parse_error_survives_a_reduce_round_trip_with_its_status_and_content_type() -> None:
    """Rebuild a ``ResponseParseError``'s status and content type."""
    error = ResponseParseError("bad", status_code=200, content_type="text/html", source="S", url="u")
    restored = copy.deepcopy(error)
    assert (restored.status_code, restored.content_type, restored.source, restored.url) == (200, "text/html", "S", "u")
    assert pickle.dumps(error)
