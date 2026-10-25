# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""``AsyncBaseClient``: construction, the transport stack, retries, the breaker, request bodies, auth, and the
declared-response pipeline.
"""

from __future__ import annotations

import inspect
import json
from contextlib import AbstractAsyncContextManager
from typing import TYPE_CHECKING, override

import httpx2
import msgspec
import pytest
from pydantic import BaseModel, ConfigDict, Field

from headgate._auth import StaticHeaderAuth
from headgate._breaker import HOST_BREAKERS
from headgate._client import AsyncBaseClient
from headgate._rate_limit import Rate, RateLimitTransport
from headgate._responses import Expect, RefusalCheck, Refused, as_bytes, nothing
from headgate._retry import Retry, RetryTransport
from headgate.errors import AuthError, RateLimitError, ResponseParseError, TransportError, UpstreamStatusError
from headgate.msgspec import decoder
from tests.support.clients import StubClient
from tests.support.responses import RESPONSE

if TYPE_CHECKING:
    from collections.abc import Callable, Generator

    from pytest_httpx2 import HTTPXMock


# --- Retry policy ---


def test_client_builds_retry_over_the_paced_transport_stack() -> None:
    client = StubClient(rate=Rate.per_second(2), base_url="https://x", max_attempts=3)
    # Retry is the outermost transport and the rate limiter the innermost. The breaker is
    # NOT a layer: it is driven from `_send` so it can span the body read as well as the
    # send, which no transport wrapped around the send can do.
    stack = client._http_client._transport
    assert isinstance(stack, RetryTransport)
    assert isinstance(stack._transport, RateLimitTransport)
    assert client._breakers is HOST_BREAKERS
    # max_attempts is the *attempt* count; the policy's total is the
    # number of *retries*, so it is one less (3 attempts -> 2 retries).
    assert stack.retry.total == 2


# --- Construction ---


def test_init_requires_base_url() -> None:
    with pytest.raises(TypeError):
        StubClient(rate=Rate.per_second(2))  # ty: ignore[missing-argument]  # base_url is required: the TypeError IS the test


def test_init_constructs_per_instance_httpx() -> None:
    a = StubClient(rate=Rate.per_second(2), base_url="https://a.example")
    b = StubClient(rate=Rate.per_second(2), base_url="https://b.example")
    assert a._http_client is not b._http_client
    assert str(a._http_client.base_url) == "https://a.example/"
    assert str(b._http_client.base_url) == "https://b.example/"


def test_init_config_not_exposed_on_self() -> None:
    """Construction arguments configure the httpx2 client and the transport stack; none is kept as an attribute."""
    client = StubClient(base_url="https://x", rate=Rate(requests=99, period=2.0))
    assert not hasattr(client, "base_url")
    assert not hasattr(client, "rate")


# --- URL joining (delegated to httpx2) ---


async def test_base_url_joins_relative_path(httpx2_mock: HTTPXMock) -> None:
    httpx2_mock.add_response(url="https://api.example/v1/foo", json={"ok": True})
    client = StubClient(rate=Rate.per_second(2), base_url="https://api.example/v1")
    resp = await client.get("foo", expect=RESPONSE)
    assert str(resp.url) == "https://api.example/v1/foo"


async def test_absolute_url_passes_through(httpx2_mock: HTTPXMock) -> None:
    httpx2_mock.add_response(url="https://other.example/raw", json={"ok": True})
    client = StubClient(rate=Rate.per_second(2), base_url="https://api.example/v1")
    resp = await client.get("https://other.example/raw", expect=RESPONSE)
    assert str(resp.url) == "https://other.example/raw"


# --- Retry on transient status ---


async def test_retries_on_503_then_succeeds(httpx2_mock: HTTPXMock) -> None:
    httpx2_mock.add_response(url="https://api.example/x", status_code=503)
    httpx2_mock.add_response(url="https://api.example/x", status_code=503)
    httpx2_mock.add_response(url="https://api.example/x", json={"ok": True})
    client = StubClient(rate=Rate.per_second(2), base_url="https://api.example", max_attempts=3)
    resp = await client.get("x", expect=RESPONSE)
    assert resp.status_code == 200


async def test_raises_after_exhausting_retries(httpx2_mock: HTTPXMock) -> None:
    for _ in range(3):
        httpx2_mock.add_response(url="https://api.example/x", status_code=503)
    client = StubClient(rate=Rate.per_second(2), base_url="https://api.example", max_attempts=3)
    # 5xx surviving all retries raises under the raise-by-default contract.
    with pytest.raises(UpstreamStatusError):
        await client.get("x", expect=RESPONSE)


# --- Retry-After honored ---


async def test_honors_retry_after_on_429(httpx2_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch) -> None:
    """A 429's ``Retry-After`` decides the wait, not the backoff curve.

    The retry transport parses the two forms the header allows, a WHOLE number of seconds or an HTTP date, and
    falls back to its own backoff for anything else. The test therefore sends a whole number: a fractional value
    would take the fallback path and prove only that a 429 is retried at all.

    The sleep is captured rather than served, so asserting the DURATION costs no wall time.
    """
    slept: list[float] = []
    real_sleep_seconds = Retry.sleep_seconds

    def _record(self: Retry, failure: httpx2.Response | Exception) -> float:
        slept.append(real_sleep_seconds(self, failure))
        return 0.0  # compute the real duration, then wait none of it

    monkeypatch.setattr(Retry, "sleep_seconds", _record)
    # Zeroed so the backoff fallback would be a visible 0.0, not a plausible-looking wait.
    monkeypatch.setattr("headgate._retry._BACKOFF_FACTOR", 0.0)
    httpx2_mock.add_response(url="https://api.example/x", status_code=429, headers={"Retry-After": "7"})
    httpx2_mock.add_response(url="https://api.example/x", json={"ok": True})

    client = StubClient(rate=Rate.per_second(2), base_url="https://api.example", max_attempts=2)
    resp = await client.get("x", expect=RESPONSE)

    assert resp.status_code == 200
    assert slept == [5.0], "the header's 7s is honoured, clamped to max_backoff_wait=5s"


# --- Circuit breaker ---


async def test_circuit_breaker_opens_after_threshold(httpx2_mock: HTTPXMock) -> None:
    # 5 consecutive failed request() calls should open the breaker for that host.
    client = StubClient(rate=Rate.per_second(2), base_url="https://api.example", max_attempts=1)
    for _ in range(5):
        httpx2_mock.add_response(url="https://api.example/x", status_code=503)
        with pytest.raises(UpstreamStatusError):
            await client.get("x", expect=RESPONSE)
    # 6th call: breaker is open, no HTTP request made — it short-circuits to a
    # TransportError ("circuit breaker open") before any request goes out.
    pre_request_count = len(httpx2_mock.get_requests())
    with pytest.raises(TransportError):
        await client.get("x", expect=RESPONSE)
    assert len(httpx2_mock.get_requests()) == pre_request_count  # no new request


async def test_circuit_breaker_resets_on_success(httpx2_mock: HTTPXMock) -> None:
    httpx2_mock.add_response(url="https://api.example/x", status_code=503)
    client = StubClient(rate=Rate.per_second(2), base_url="https://api.example", max_attempts=1)
    with pytest.raises(UpstreamStatusError):
        await client.get("x", expect=RESPONSE)  # 1 failure
    httpx2_mock.add_response(url="https://api.example/x", json={"ok": True})
    await client.get("x", expect=RESPONSE)
    # Breaker should be reset; counter back to 0.
    host = "api.example"
    assert host in HOST_BREAKERS, "breaker should be registered for the host after a fetch"
    assert HOST_BREAKERS[host].failure_count == 0


# --- request bodies ---


def _req_body_model() -> Callable[..., BaseModel]:
    class _ReqBody(BaseModel):
        model_config = ConfigDict(populate_by_name=True)
        order_side: str = Field(serialization_alias="orderSide")
        stop_price: float | None = None

    return _ReqBody


async def test_json_body_kwarg_rides_through_with_content_type(httpx2_mock: HTTPXMock) -> None:
    # json=<builtins> rides **kwargs; httpx2 serializes it and stamps the content-type. The caller owns the
    # builtins (here: a pydantic model_dump with aliases and exclude_none).
    httpx2_mock.add_response(url="https://api.example/o", json={"ok": True})
    client = StubClient(rate=Rate.per_second(2), base_url="https://api.example")
    body = _req_body_model()(order_side="buy").model_dump(by_alias=True, exclude_none=True)
    await client.post("o", expect=RESPONSE, json=body)
    req = httpx2_mock.get_requests()[0]
    assert req.headers["content-type"] == "application/json"
    # by_alias applied (orderSide), exclude_none drops stop_price
    assert json.loads(req.content) == {"orderSide": "buy"}


async def test_json_body_content_type_is_overridable(httpx2_mock: HTTPXMock) -> None:
    # An explicit caller Content-Type wins over the json= stamp.
    httpx2_mock.add_response(url="https://api.example/o", json={})
    client = StubClient(rate=Rate.per_second(2), base_url="https://api.example")
    body = _req_body_model()(order_side="buy").model_dump(by_alias=True, exclude_none=True)
    await client.post(
        "o",
        expect=RESPONSE,
        json=body,
        headers={"Content-Type": "application/vnd.api+json"},
    )
    req = httpx2_mock.get_requests()[0]
    assert req.headers["content-type"] == "application/vnd.api+json"


async def test_json_body_does_not_mutate_caller_headers(httpx2_mock: HTTPXMock) -> None:
    # httpx2 copies caller headers into its own Headers, so the json= path never mutates the caller's dict.
    httpx2_mock.add_response(url="https://api.example/o", json={})
    client = StubClient(rate=Rate.per_second(2), base_url="https://api.example")
    caller_headers: dict[str, str] = {"X-Custom": "1"}
    body = _req_body_model()(order_side="buy").model_dump(by_alias=True, exclude_none=True)
    await client.post("o", expect=RESPONSE, json=body, headers=caller_headers)
    assert "Content-Type" not in caller_headers  # httpx2 copied, not mutated


# --- verb: per-call timeout is a named param ---


async def test_timeout_named_param_forwarded_to_httpx(httpx2_mock: HTTPXMock) -> None:
    captured: dict[str, object] = {}

    def handler(req: httpx2.Request) -> httpx2.Response:
        captured["timeout"] = req.extensions.get("timeout")
        return httpx2.Response(200, json={})

    client = StubClient(
        rate=Rate.per_second(2), base_url="https://api.example", transport=httpx2.MockTransport(handler)
    )
    await client.get("x", expect=RESPONSE, timeout=5.0)
    assert captured["timeout"] == {"connect": 5.0, "read": 5.0, "write": 5.0, "pool": 5.0}


# --- Non-transient 4xx is not retried ---


async def test_non_transient_4xx_raises_after_one_request(httpx2_mock: HTTPXMock) -> None:
    """A non-transient 4xx (e.g. 404) is not retried and raises with its real status."""
    httpx2_mock.add_response(url="https://api.example/x", status_code=404)
    client = StubClient(rate=Rate.per_second(2), base_url="https://api.example", max_attempts=3)
    with pytest.raises(UpstreamStatusError) as ei:
        await client.get("x", expect=RESPONSE)
    assert ei.value.status_code == 404
    assert len(httpx2_mock.get_requests()) == 1


async def test_403_raises_auth_error_after_one_request(httpx2_mock: HTTPXMock) -> None:
    """403 is non-transient and names OUR credential."""
    httpx2_mock.add_response(url="https://api.example/x", status_code=403)
    client = StubClient(rate=Rate.per_second(2), base_url="https://api.example", max_attempts=3)
    with pytest.raises(AuthError) as ei:
        await client.get("x", expect=RESPONSE)
    assert ei.value.status_code == 403
    assert len(httpx2_mock.get_requests()) == 1


# --- The reported failure reflects the actual last attempt ---


async def test_reported_failure_reflects_the_last_attempt_kind(httpx2_mock: HTTPXMock) -> None:
    """Mixed transient-then-network failure: the raised error describes the final failure, not an earlier 503.

    The retry layer records each attempt's outcome; the ConnectError that ended the call must not be overwritten by
    the 503 that preceded it. The property is checked on the caller-visible exception, so it cannot pass while what
    a caller sees is wrong.
    """
    httpx2_mock.add_response(url="https://api.example/x", status_code=503)
    httpx2_mock.add_exception(httpx2.ConnectError("net"))
    httpx2_mock.add_exception(httpx2.ConnectError("net"))
    client = StubClient(rate=Rate.per_second(2), base_url="https://api.example", max_attempts=3)
    with pytest.raises(TransportError) as ei:
        await client.get("x", expect=RESPONSE)

    detail = str(ei.value)
    assert "net" in detail, f"Expected ConnectError 'net' in the raised error but got: {detail!r}"
    assert "503" not in detail, f"Stale 503 leaked into the raised error: {detail!r}"
    # The message carries the request line with the RELATIVE path the caller passed (``x``); the client name in
    # the ``source`` prefix identifies the host.
    assert "GET x" in detail, f"Request line missing from the error: {detail!r}"


# --- an undeclared 4xx carries its body ---


async def test_an_undeclared_4xx_folds_its_body_into_the_message(httpx2_mock: HTTPXMock) -> None:
    """A non-transient 4xx the call did not declare folds the response body into the error message: the
    upstream's own reason is the diagnosis, and without it the failure has to be reproduced just to read it.
    """

    class M(msgspec.Struct):
        a: int

    httpx2_mock.add_response(
        url="https://api.example/x",
        status_code=400,
        content=b'{"code":3003,"message":"insufficient balance to cover this order"}',
    )
    client = StubClient(rate=Rate.per_second(2), base_url="https://api.example")
    with pytest.raises(UpstreamStatusError) as ei:
        await client.get("x", expect=decoder(M))
    assert "insufficient balance to cover this order" in str(ei.value)


async def test_an_undeclared_403_folds_its_body_into_the_message(httpx2_mock: HTTPXMock) -> None:
    """The 401/403 branch carries the body too: an auth body says WHY the credential was refused."""

    class M(msgspec.Struct):
        a: int

    httpx2_mock.add_response(url="https://api.example/x", status_code=403, content=b'{"message":"token expired"}')
    client = StubClient(rate=Rate.per_second(2), base_url="https://api.example")
    with pytest.raises(AuthError) as ei:
        await client.get("x", expect=decoder(M))
    assert "token expired" in str(ei.value)


async def test_4xx_body_snippet_is_bounded_and_single_line(httpx2_mock: HTTPXMock) -> None:
    """The folded body is a bounded, whitespace-collapsed snippet — a 4xx HTML
    error page must not turn the message into a payload.
    """

    class M(msgspec.Struct):
        a: int

    httpx2_mock.add_response(
        url="https://api.example/x",
        status_code=400,
        content=b"<html>\n" + b"x" * 5000 + b"\nTAIL-MARKER</html>",
    )
    client = StubClient(rate=Rate.per_second(2), base_url="https://api.example")
    with pytest.raises(UpstreamStatusError) as ei:
        await client.get("x", expect=decoder(M))
    msg = str(ei.value)
    assert "TAIL-MARKER" not in msg
    assert len(msg) < 500
    assert "\n" not in msg


# --- auth ---


async def test_no_auth_no_authorization_header(httpx2_mock: HTTPXMock) -> None:
    """A client built without ``auth=`` sends no Authorization header."""
    httpx2_mock.add_response(url="https://api.example/x", json={})
    client = StubClient(rate=Rate.per_second(2), base_url="https://api.example")
    await client.get("x", expect=RESPONSE)
    req = httpx2_mock.get_requests()[0]
    assert "Authorization" not in req.headers


async def test_explicit_headers_win_over_auth(httpx2_mock: HTTPXMock) -> None:
    """An explicit ``headers=`` value wins a clash against the Auth flow (``StaticHeaderAuth`` uses setdefault)."""
    httpx2_mock.add_response(url="https://api.example/x", json={})
    client = StubClient(
        rate=Rate.per_second(2),
        base_url="https://api.example",
        auth=StaticHeaderAuth({"Authorization": "Bearer T"}),
    )
    await client.get("x", expect=RESPONSE, headers={"Authorization": "X"})
    req = httpx2_mock.get_requests()[0]
    assert req.headers.get("Authorization") == "X"


async def test_auth_param_threaded_to_async_client(httpx2_mock: HTTPXMock) -> None:
    """A ``httpx2.Auth`` passed as ``auth=`` runs at send time and stamps the
    request — proving the base threads it into the underlying AsyncClient.
    """

    class _StampAuth(httpx2.Auth):
        @override
        def auth_flow(self, request: httpx2.Request) -> Generator[httpx2.Request, httpx2.Response]:
            request.headers.setdefault("X-A", "1")
            yield request

    httpx2_mock.add_response(url="https://api.example/x", json={})
    client = StubClient(rate=Rate.per_second(2), base_url="https://api.example", auth=_StampAuth())
    await client.get("x", expect=RESPONSE)
    req = httpx2_mock.get_requests()[0]
    assert req.headers["X-A"] == "1"


# --- AsyncBaseClient is an AbstractAsyncContextManager ---


def test_async_base_client_is_abstract_async_context_manager() -> None:
    """AsyncBaseClient inherits the stdlib protocol — ``async with``
    works without us writing the methods, and isinstance checks pass.
    """
    client = StubClient(rate=Rate.per_second(2), base_url="https://a.example")
    assert isinstance(client, AbstractAsyncContextManager)


# --- msgspec strictness: decoder(strict=) ---


async def test_struct_decode_lax_optin_coerces_strings() -> None:
    """strict=False (an explicit opt-out; the default is strict) coerces string-quoted numbers on the wire into
    number-typed struct fields.
    """

    class _Px(msgspec.Struct):
        price: float | None = None

    def handler(req: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, content=b'{"price":"59695.24"}')

    client = StubClient(
        rate=Rate.per_second(2), base_url="https://api.example", transport=httpx2.MockTransport(handler)
    )
    out = await client.get("x", expect=decoder(_Px, strict=False))
    assert out.price == 59695.24


async def test_struct_decode_strict_rejects_string_quoted_numbers() -> None:
    """strict=True makes number-typed wires exact-or-fail: a string-quoted
    number raises ResponseParseError instead of coercing.
    """

    class _Px(msgspec.Struct):
        price: float | None = None

    def handler(req: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, content=b'{"price":"59695.24"}')

    client = StubClient(
        rate=Rate.per_second(2),
        base_url="https://api.example",
        transport=httpx2.MockTransport(handler),
    )
    with pytest.raises(ResponseParseError):
        await client.get("x", expect=decoder(_Px, strict=True))


async def test_struct_decode_strict_accepts_integer_tokens_for_float_fields() -> None:
    """strict=True still decodes JSON *integer* tokens into float fields —
    a number is a number.
    """

    class _Px(msgspec.Struct):
        oi: float | None = None

    def handler(req: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, content=b'{"oi":837345900}')

    client = StubClient(
        rate=Rate.per_second(2), base_url="https://api.example", transport=httpx2.MockTransport(handler)
    )
    out = await client.get("x", expect=decoder(_Px, strict=True))
    assert out.oi == 837345900.0


# --- declared-response helpers ---


class _Answer(msgspec.Struct, frozen=True):
    message: str


class _Value(msgspec.Struct, frozen=True):
    x: int


class _Status(msgspec.Struct, frozen=True):
    status: str


def _serving(
    *responses: httpx2.Response, follow_redirects: bool = True, refusal_check: RefusalCheck | None = None
) -> StubClient:
    it = iter(responses)
    return StubClient(
        rate=Rate.per_second(2),
        base_url="https://api.example",
        transport=httpx2.MockTransport(lambda req: next(it)),
        follow_redirects=follow_redirects,
        refusal_check=refusal_check,
    )


# --- refusal: the host's own check, ahead of everything the call declared ---


def _key_page_refusal(resp: httpx2.Response) -> Refused | None:
    if resp.is_redirect and resp.headers.get("location", "").endswith("missing_key.html"):
        return Refused("rejected credential", kind=AuthError, code="missing_key")
    return None


async def test_a_host_refusal_is_raised_as_its_kind_with_the_real_status() -> None:
    """A key-error 302: the check names the refusal, and the error carries the 302 the wire sent.

    It is built by ``refusal_error``, as a reader's refusal is: the provider's code, the client's name as the source,
    and the body snippet all ride along. A hand-built ``refused.kind(refused.message, status_code=...)`` keeps the
    kind and the status and drops the other three.
    """
    client = _serving(
        httpx2.Response(
            302, headers={"Location": "https://api.example/missing_key.html"}, content=b"Moved to key page"
        ),
        follow_redirects=False,
        refusal_check=_key_page_refusal,
    )
    with pytest.raises(AuthError) as ei:
        await client.get("x", expect=decoder(dict[str, str]))
    assert ei.value.status_code == 302
    assert ei.value.code == "missing_key"
    assert ei.value.source == "StubClient"
    assert "Moved to key page" in str(ei.value)


async def test_a_host_check_returning_none_changes_nothing_and_sees_every_response() -> None:
    """The host check is a check, not a replacement.

    Returning ``None`` lets a 2xx decode and a non-2xx raise as before.
    """
    seen: list[int] = []

    def refusal(resp: httpx2.Response) -> Refused | None:
        seen.append(resp.status_code)
        return None

    client = _serving(
        httpx2.Response(200, content=b'{"ok":"yes"}'),
        httpx2.Response(404),
        follow_redirects=False,
        refusal_check=refusal,
    )
    assert await client.get("x", expect=decoder(dict[str, str])) == {"ok": "yes"}
    with pytest.raises(UpstreamStatusError) as ei:
        await client.get("x", expect=decoder(dict[str, str]))
    assert ei.value.status_code == 404
    assert seen == [200, 404]  # it saw BOTH — the success too, and before the ladder decided anything


async def test_the_host_check_runs_before_a_declared_answer() -> None:
    """The host step precedes the endpoint step: a host refusal wins even on a status the call declared.

    The declared reader for 400 never returns normally — it raises a DIFFERENT refusal (``RateLimitError``) and
    records every status it is asked to read. A mutant that runs the endpoint step before the host step is
    caught two ways: the raised type would be ``RateLimitError`` instead of ``AuthError``, and ``seen`` would be
    non-empty because the reader ran at all. A reader that merely returned a value could not tell the two orderings
    apart, since the host's ``AuthError`` would propagate either way.
    """
    seen: list[int] = []

    def declared_reader(resp: httpx2.Response) -> _Answer:
        seen.append(resp.status_code)
        msg = "a different refusal, if this ever runs"
        raise Refused(msg, kind=RateLimitError)

    def refusal(resp: httpx2.Response) -> Refused | None:
        return Refused("host refused", kind=AuthError) if resp.status_code == 400 else None

    client = _serving(httpx2.Response(400, json={"message": "refused"}), follow_redirects=False, refusal_check=refusal)
    with pytest.raises(AuthError) as ei:
        await client.post("x", expect=Expect(decoder(_Value)).on(400, declared_reader))
    assert ei.value.status_code == 400

    assert seen == []


def test_a_refusing_client_that_follows_redirects_is_refused_at_construction() -> None:
    """The check sees only the final response, so a refusal on a followed hop would be skipped."""
    with pytest.raises(ValueError, match="follow_redirects=False"):
        StubClient(rate=Rate.per_second(2), base_url="https://api.example", refusal_check=_key_page_refusal)


async def test_a_per_call_follow_redirects_on_a_refusing_client_is_refused() -> None:
    client = _serving(httpx2.Response(200, content=b"{}"), follow_redirects=False, refusal_check=_key_page_refusal)
    with pytest.raises(ValueError, match="follow redirects"):
        await client.get("x", expect=decoder(dict[str, str]), follow_redirects=True)


# --- declared responses: the pipeline ---


async def test_a_declared_non_2xx_reaches_its_reader() -> None:
    client = _serving(httpx2.Response(400, json={"message": "refused"}))
    out = await client.post("x", expect=Expect(decoder(_Value)).on(400, decoder(_Answer)))
    assert out == _Answer(message="refused")


async def test_an_exact_2xx_declaration_wins_over_the_success_reader() -> None:
    client = _serving(httpx2.Response(204))
    assert await client.post("x", expect=Expect(decoder(_Value)).on(204, nothing)) is None


async def test_a_declared_non_2xx_in_an_unrecognised_shape_falls_through_to_the_ladder() -> None:
    """Told no in a shape we do not know: the real status, never a parse error."""
    client = _serving(httpx2.Response(400, content=b"<html>gateway</html>"))
    with pytest.raises(UpstreamStatusError) as ei:
        await client.post("x", expect=Expect(decoder(_Value)).on(400, decoder(_Answer)))
    assert ei.value.status_code == 400
    assert type(ei.value) is UpstreamStatusError


async def test_a_2xx_that_does_not_decode_is_a_parse_error() -> None:
    client = _serving(httpx2.Response(200, content=b"not json"))
    with pytest.raises(ResponseParseError):
        await client.get("x", expect=decoder(_Value))


async def test_an_undeclared_status_raises_even_beside_a_declared_one() -> None:
    client = _serving(httpx2.Response(404, json={"message": "gone"}))
    with pytest.raises(UpstreamStatusError) as ei:
        await client.post("x", expect=Expect(decoder(_Value)).on(400, decoder(_Answer)))
    assert ei.value.status_code == 404


async def test_nothing_returns_none_for_a_2xx_whatever_its_body() -> None:
    client = _serving(httpx2.Response(200, content=b"Cancel request was submitted"))
    assert await client.delete("x", expect=nothing) is None


async def test_a_refused_reader_becomes_its_kind_with_the_real_status_and_code() -> None:
    """A refusal written in a 200 body carries 200 — never an invented status."""

    def check(env: _Status) -> Refused | None:
        return Refused("quota spent", code=env.status, kind=RateLimitError) if env.status != "OK" else None

    client = _serving(httpx2.Response(200, json={"status": "REQUEST_NOT_PROCESSED"}))
    with pytest.raises(RateLimitError) as ei:
        await client.get("x", expect=decoder(_Status, check=check))
    assert (ei.value.status_code, ei.value.code, ei.value.source) == (200, "REQUEST_NOT_PROCESSED", "StubClient")
    assert "quota spent" in str(ei.value)


async def test_a_refusal_from_a_declared_non_2xx_reader_keeps_that_status() -> None:
    def refusing(resp: httpx2.Response) -> _Answer:
        msg = "the provider refused, in its own words"
        raise Refused(msg, code="E42")

    client = _serving(httpx2.Response(409, content=b"{}"))
    with pytest.raises(UpstreamStatusError) as ei:
        await client.post("x", expect=Expect(decoder(_Value)).on(409, refusing))
    assert (ei.value.status_code, ei.value.code) == (409, "E42")


async def test_a_status_error_never_carries_a_query_string_secret() -> None:
    """httpx2's own ``raise_for_status()`` puts the full URL, api_key included, in its message.

    Some APIs authenticate by query parameter, and a ``ClientError`` message is routinely logged or shown, so the
    ladder builds its own message from ``sanitize_url``. "Simplifying" ``status_error`` to
    ``resp.raise_for_status()`` must fail here.
    """
    client = _serving(httpx2.Response(404, content=b"gone"))
    with pytest.raises(UpstreamStatusError) as ei:
        await client.get("series?series_id=GDP&api_key=SECRET123", expect=as_bytes)
    assert "SECRET123" not in str(ei.value)
    assert "api_key=%2A%2A%2A" in str(ei.value)


async def test_a_refused_reader_never_carries_a_query_string_secret() -> None:
    """Mirrors the ladder's own secret-redaction test above: ``refusal_error`` builds its message from
    ``sanitize_url`` too, not from the raw URL.
    """

    def refusing(resp: httpx2.Response) -> _Value:
        msg = "the provider refused"
        raise Refused(msg)

    client = _serving(httpx2.Response(200, content=b"{}"))
    with pytest.raises(UpstreamStatusError) as ei:
        await client.get("series?series_id=GDP&api_key=SECRET123", expect=refusing)
    assert "SECRET123" not in str(ei.value)
    assert "api_key=%2A%2A%2A" in str(ei.value)


async def test_a_declared_403_in_an_unrecognised_shape_raises_auth_error() -> None:
    """The fall-through goes through the real ladder (``status_error``), not a bare ``UpstreamStatusError`` —
    a declared 403 that doesn't decode still raises ``AuthError``, the 401/403 branch of the same ladder the
    default layer uses.
    """
    client = _serving(httpx2.Response(403, content=b"<html>denied</html>"))
    with pytest.raises(AuthError) as ei:
        await client.post("x", expect=Expect(decoder(_Value)).on(403, decoder(_Answer)))
    assert ei.value.status_code == 403


# --- Constructor signature ---


def test_rate_is_a_required_keyword_only_constructor_argument() -> None:
    """A client that states no rate is a ``TypeError``, not a budget nobody chose."""
    rate = inspect.signature(AsyncBaseClient.__init__).parameters["rate"]

    assert rate.kind is inspect.Parameter.KEYWORD_ONLY
    assert rate.default is inspect.Parameter.empty
