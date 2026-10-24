"""Tests for the new AsyncBaseClient (post-refactor)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, override

import httpx2
import msgspec
import pytest

from headgate._async_breaker import _host_breakers
from headgate._auth import StaticHeaderAuth
from headgate._base import AsyncBaseClient
from headgate._rate_limiter import Rate
from headgate._responses import Expect, Refusal, Refused, as_bytes, nothing
from headgate._transports import Retry
from headgate.errors import AuthError, RateLimitError, ResponseParseError, UpstreamStatusError
from headgate.msgspec import decoder
from tests.support.responses import RESPONSE

if TYPE_CHECKING:
    from collections.abc import Callable, Generator

    from pydantic import BaseModel
    from pytest_httpx import HTTPXMock


class _StubClient(AsyncBaseClient):
    """Minimal subclass used for tests."""


# ---------- Retry policy ----------


def test_client_builds_retry_over_the_paced_transport_stack() -> None:
    from headgate._transports import RateLimitTransport, RetryTransport

    client = _StubClient(rate=Rate.per_second(2), base_url="https://x", max_retries=3)
    # Retry is the outermost transport and the rate limiter the innermost. The breaker is
    # NOT a layer: it is driven from `_send` so it can span the body read as well as the
    # send, which no transport wrapped around the send can do.
    stack = client._http_client._transport
    assert isinstance(stack, RetryTransport)
    assert isinstance(stack._async_transport, RateLimitTransport)
    assert client._breakers is _host_breakers
    # max_retries is the *attempt* count; the policy's total is the
    # number of *retries*, so it is one less (3 attempts -> 2 retries).
    assert stack.retry.total == 2


# ---------- Construction ----------


def test_init_requires_base_url() -> None:
    with pytest.raises(TypeError):
        _StubClient(rate=Rate.per_second(2))  # ty: ignore[missing-argument]  # base_url is required: the TypeError IS the test


def test_init_constructs_per_instance_httpx() -> None:
    a = _StubClient(rate=Rate.per_second(2), base_url="https://a.example")
    b = _StubClient(rate=Rate.per_second(2), base_url="https://b.example")
    assert a._http_client is not b._http_client
    assert str(a._http_client.base_url) == "https://a.example/"
    assert str(b._http_client.base_url) == "https://b.example/"


def test_init_config_not_exposed_on_self() -> None:
    client = _StubClient(base_url="https://x", rate=Rate(requests=99, period=2.0))
    # The class exposes no base_url, no rate, no max_rate, no rate_period on the instance.
    assert not hasattr(client, "base_url")
    assert not hasattr(client, "rate")
    assert not hasattr(client, "max_rate")
    assert not hasattr(client, "rate_period")
    assert not hasattr(client, "Config")


# ---------- URL joining (delegated to httpx2) ----------


async def test_base_url_joins_relative_path(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(url="https://api.example/v1/foo", json={"ok": True})
    client = _StubClient(rate=Rate.per_second(2), base_url="https://api.example/v1")
    resp = await client.get("foo", expect=RESPONSE)
    assert resp is not None
    assert str(resp.url) == "https://api.example/v1/foo"


async def test_absolute_url_passes_through(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(url="https://other.example/raw", json={"ok": True})
    client = _StubClient(rate=Rate.per_second(2), base_url="https://api.example/v1")
    resp = await client.get("https://other.example/raw", expect=RESPONSE)
    assert resp is not None
    assert str(resp.url) == "https://other.example/raw"


# ---------- Retry on transient status ----------


async def test_retries_on_503_then_succeeds(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(url="https://api.example/x", status_code=503)
    httpx_mock.add_response(url="https://api.example/x", status_code=503)
    httpx_mock.add_response(url="https://api.example/x", json={"ok": True})
    client = _StubClient(rate=Rate.per_second(2), base_url="https://api.example", max_retries=3)
    resp = await client.get("x", expect=RESPONSE)
    assert resp is not None
    assert resp.status_code == 200


async def test_raises_after_exhausting_retries(httpx_mock: HTTPXMock) -> None:
    from headgate.errors import UpstreamStatusError

    for _ in range(3):
        httpx_mock.add_response(url="https://api.example/x", status_code=503)
    client = _StubClient(rate=Rate.per_second(2), base_url="https://api.example", max_retries=3)
    # 5xx surviving all retries raises under the raise-by-default contract.
    with pytest.raises(UpstreamStatusError):
        await client.get("x", expect=RESPONSE)


# ---------- Retry-After honored ----------


async def test_honors_retry_after_on_429(httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch) -> None:
    """A 429's ``Retry-After`` decides the wait, not the backoff curve.

    The header value must be a WHOLE number of seconds (or an HTTP date): the retry transport
    parses those two forms (as httpx-retries did) and rejects anything else, falling back to its own backoff. This
    test used to send ``Retry-After: 0.01`` and so proved only that a 429 is retried at all —
    the rejected value took the fallback path and the header was never honoured.

    The sleep is captured rather than served, so asserting the DURATION costs no wall time.
    """
    slept: list[float] = []
    real_sleep_seconds = Retry.sleep_seconds

    def _record(self: Retry, failure: httpx2.Response | Exception) -> float:
        slept.append(real_sleep_seconds(self, failure))
        return 0.0  # compute the real duration, then wait none of it

    monkeypatch.setattr(Retry, "sleep_seconds", _record)
    # Zeroed so the backoff fallback would be a visible 0.0, not a plausible-looking wait.
    monkeypatch.setattr("headgate._transports._BACKOFF_FACTOR", 0.0)
    httpx_mock.add_response(url="https://api.example/x", status_code=429, headers={"Retry-After": "7"})
    httpx_mock.add_response(url="https://api.example/x", json={"ok": True})

    client = _StubClient(rate=Rate.per_second(2), base_url="https://api.example", max_retries=2)
    resp = await client.get("x", expect=RESPONSE)

    assert resp is not None
    assert resp.status_code == 200
    assert slept == [5.0], "the header's 7s is honoured, clamped to max_backoff_wait=5s"


# ---------- Circuit breaker ----------


async def test_circuit_breaker_opens_after_threshold(httpx_mock: HTTPXMock) -> None:
    from headgate.errors import TransportError, UpstreamStatusError

    # 5 consecutive failed request() calls should open the breaker for that host.
    client = _StubClient(rate=Rate.per_second(2), base_url="https://api.example", max_retries=1)
    for _ in range(5):
        httpx_mock.add_response(url="https://api.example/x", status_code=503)
        with pytest.raises(UpstreamStatusError):
            await client.get("x", expect=RESPONSE)
    # 6th call: breaker is open, no HTTP request made — it short-circuits to a
    # TransportError ("circuit breaker open") before any request goes out.
    pre_request_count = len(httpx_mock.get_requests())
    with pytest.raises(TransportError):
        await client.get("x", expect=RESPONSE)
    assert len(httpx_mock.get_requests()) == pre_request_count  # no new request


async def test_circuit_breaker_resets_on_success(httpx_mock: HTTPXMock) -> None:
    from headgate.errors import UpstreamStatusError

    httpx_mock.add_response(url="https://api.example/x", status_code=503)
    client = _StubClient(rate=Rate.per_second(2), base_url="https://api.example", max_retries=1)
    with pytest.raises(UpstreamStatusError):
        await client.get("x", expect=RESPONSE)  # 1 failure
    httpx_mock.add_response(url="https://api.example/x", json={"ok": True})
    resp = await client.get("x", expect=RESPONSE)
    assert resp is not None
    # Breaker should be reset; counter back to 0.
    host = "api.example"
    assert host in _host_breakers, "breaker should be registered for the host after a fetch"
    assert _host_breakers[host].failure_count == 0


# ---------- get: JSON + model decode paths ----------


async def test_request_returns_json(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(url="https://api.example/x", json={"a": 1})
    client = _StubClient(rate=Rate.per_second(2), base_url="https://api.example")
    result = await client.get("x", expect=decoder(dict[str, Any]))
    assert result == {"a": 1}


async def test_request_raises_on_decode_error(httpx_mock: HTTPXMock) -> None:
    from headgate.errors import ResponseParseError

    httpx_mock.add_response(url="https://api.example/x", content=b"not json")
    client = _StubClient(rate=Rate.per_second(2), base_url="https://api.example")
    with pytest.raises(ResponseParseError):
        await client.get("x", expect=decoder(dict[str, Any]))


async def test_request_validates_model(httpx_mock: HTTPXMock) -> None:
    import msgspec

    class M(msgspec.Struct):
        a: int

    httpx_mock.add_response(url="https://api.example/x", json={"a": 1})
    client = _StubClient(rate=Rate.per_second(2), base_url="https://api.example")
    result = await client.get("x", expect=decoder(M))
    assert result is not None
    assert result.a == 1


# ---------- post: serialize (request-body model) ----------


def _req_body_model() -> Callable[..., BaseModel]:
    from pydantic import BaseModel, ConfigDict, Field

    class _ReqBody(BaseModel):
        model_config = ConfigDict(populate_by_name=True)
        order_side: str = Field(serialization_alias="orderSide")
        stop_price: float | None = None

    return _ReqBody


async def test_json_body_kwarg_rides_through_with_content_type(httpx_mock: HTTPXMock) -> None:
    # Bodies are httpx-native now: json=<builtins> rides **kwargs, httpx2
    # serializes and stamps the content-type. The caller owns the builtins
    # (here: pydantic model_dump with aliases + exclude_none).
    import json

    httpx_mock.add_response(url="https://api.example/o", json={"ok": True})
    client = _StubClient(rate=Rate.per_second(2), base_url="https://api.example")
    body = _req_body_model()(order_side="buy").model_dump(by_alias=True, exclude_none=True)
    await client.post("o", expect=RESPONSE, json=body)
    req = httpx_mock.get_requests()[0]
    assert req.headers["content-type"] == "application/json"
    # by_alias applied (orderSide), exclude_none drops stop_price
    assert json.loads(req.content) == {"orderSide": "buy"}


async def test_json_body_content_type_is_overridable(httpx_mock: HTTPXMock) -> None:
    # httpx-native behavior (verified): an explicit caller Content-Type wins
    # over the json= stamp — the same contract the old serialize= seam had.
    httpx_mock.add_response(url="https://api.example/o", json={})
    client = _StubClient(rate=Rate.per_second(2), base_url="https://api.example")
    body = _req_body_model()(order_side="buy").model_dump(by_alias=True, exclude_none=True)
    await client.post(
        "o",
        expect=RESPONSE,
        json=body,
        headers={"Content-Type": "application/vnd.api+json"},
    )
    req = httpx_mock.get_requests()[0]
    assert req.headers["content-type"] == "application/vnd.api+json"


async def test_json_body_does_not_mutate_caller_headers(httpx_mock: HTTPXMock) -> None:
    # httpx-native behavior (verified): httpx2 copies caller headers into its
    # own Headers — the json= path never mutates the caller's dict.
    httpx_mock.add_response(url="https://api.example/o", json={})
    client = _StubClient(rate=Rate.per_second(2), base_url="https://api.example")
    caller_headers: dict[str, str] = {"X-Custom": "1"}
    body = _req_body_model()(order_side="buy").model_dump(by_alias=True, exclude_none=True)
    await client.post("o", expect=RESPONSE, json=body, headers=caller_headers)
    assert "Content-Type" not in caller_headers  # httpx2 copied, not mutated


# ---------- verb: per-call timeout is a named param ----------


async def test_timeout_named_param_forwarded_to_httpx(httpx_mock: HTTPXMock) -> None:
    captured: dict[str, object] = {}

    def handler(req: httpx2.Request) -> httpx2.Response:
        captured["timeout"] = req.extensions.get("timeout")
        return httpx2.Response(200, json={})

    client = _StubClient(
        rate=Rate.per_second(2), base_url="https://api.example", transport=httpx2.MockTransport(handler)
    )
    await client.get("x", expect=RESPONSE, timeout=5.0)
    assert captured["timeout"] == {"connect": 5.0, "read": 5.0, "write": 5.0, "pool": 5.0}


# ---------- Non-transient 4xx passthrough ----------


async def test_non_transient_4xx_raises_after_one_request(httpx_mock: HTTPXMock) -> None:
    """A non-transient 4xx (e.g. 404) is not retried and raises with its real status."""
    httpx_mock.add_response(url="https://api.example/x", status_code=404)
    client = _StubClient(rate=Rate.per_second(2), base_url="https://api.example", max_retries=3)
    with pytest.raises(UpstreamStatusError) as ei:
        await client.get("x", expect=RESPONSE)
    assert ei.value.status_code == 404
    assert len(httpx_mock.get_requests()) == 1


async def test_403_raises_auth_error_after_one_request(httpx_mock: HTTPXMock) -> None:
    """403 is non-transient and names OUR credential."""
    httpx_mock.add_response(url="https://api.example/x", status_code=403)
    client = _StubClient(rate=Rate.per_second(2), base_url="https://api.example", max_retries=3)
    with pytest.raises(AuthError) as ei:
        await client.get("x", expect=RESPONSE)
    assert ei.value.status_code == 403
    assert len(httpx_mock.get_requests()) == 1


# ---------- The reported failure reflects the actual last attempt ----------


# The per-call limiter override is GONE (the limiter is a transport now, and a request
# extension cannot survive the cache layer to reach it). A client's pacing is its
# construction-time ``rate`` plus its ``rate_limits`` path rules; both are exercised
# behaviourally in ``test_rate_limit_transport.py``.


async def test_reported_failure_reflects_the_LAST_attempt_kind(httpx_mock: HTTPXMock) -> None:
    """Mixed transient-then-network failure: the raised error must describe the *actual*
    final failure, not a stale earlier 503.

    Regression test for the ``set_result(resp)``-after-``with attempt:`` bug: that pattern
    unconditionally overwrote the retry layer's recorded exception when a later attempt
    raised a network error, so the reported failure named a stale 503 instead of the
    ConnectError that actually ended the call.

    The property is checked on the CALLER-VISIBLE exception, so it cannot pass while the
    thing a caller actually sees is wrong.
    """
    from headgate.errors import TransportError

    httpx_mock.add_response(url="https://api.example/x", status_code=503)
    httpx_mock.add_exception(httpx2.ConnectError("net"))
    httpx_mock.add_exception(httpx2.ConnectError("net"))
    client = _StubClient(rate=Rate.per_second(2), base_url="https://api.example", max_retries=3)
    with pytest.raises(TransportError) as ei:
        await client.get("x", expect=RESPONSE)

    detail = str(ei.value)
    assert "net" in detail, f"Expected ConnectError 'net' in the raised error but got: {detail!r}"
    assert "503" not in detail, f"Stale 503 leaked into the raised error: {detail!r}"
    # The request line rode a log field until the logger went; it is in the message now.
    # The path is the RELATIVE one the caller passed (``x``), which is what the old log
    # field carried too — the client name in the ``source`` prefix supplies the host.
    assert "GET x" in detail, f"Request line missing from the error: {detail!r}"


# ---------- no None return: an undeclared status always raises ----------


async def test_model_404_raises(httpx_mock: HTTPXMock) -> None:
    """A 404 the call did not declare raises UpstreamStatusError — there is no no-data
    carve-out, and the raise reaches the caller (the ``none_on_status`` seam that mapped it was deleted."""

    import msgspec

    from headgate.errors import UpstreamStatusError

    class M(msgspec.Struct):
        a: int

    httpx_mock.add_response(url="https://api.example/x", status_code=404)
    client = _StubClient(rate=Rate.per_second(2), base_url="https://api.example")
    with pytest.raises(UpstreamStatusError) as ei:
        await client.get("x", expect=decoder(M))
    assert ei.value.status_code == 404


async def test_4xx_decode_mode_folds_body_into_message(httpx_mock: HTTPXMock) -> None:
    """A non-transient 4xx the call did not declare folds the response body into the error
    message — the upstream's own reason IS the diagnosis (Public's preflight 400
    carries {"code":3003,"message":"You need an additional $1,371.58 …"}), and
    dropping it forced a manual reproduction just to read it."""

    import msgspec

    from headgate.errors import UpstreamStatusError

    class M(msgspec.Struct):
        a: int

    httpx_mock.add_response(
        url="https://api.example/x",
        status_code=400,
        content=b'{"code":3003,"message":"You need an additional $1,371.58 to cover this order."}',
    )
    client = _StubClient(rate=Rate.per_second(2), base_url="https://api.example")
    with pytest.raises(UpstreamStatusError) as ei:
        await client.get("x", expect=decoder(M))
    assert "You need an additional $1,371.58" in str(ei.value)


async def test_403_decode_mode_folds_body_into_message(httpx_mock: HTTPXMock) -> None:
    """The 401/403 branch of the same block carries the body too — an auth
    body says WHY the credential was refused."""

    import msgspec

    from headgate.errors import AuthError

    class M(msgspec.Struct):
        a: int

    httpx_mock.add_response(url="https://api.example/x", status_code=403, content=b'{"message":"token expired"}')
    client = _StubClient(rate=Rate.per_second(2), base_url="https://api.example")
    with pytest.raises(AuthError) as ei:
        await client.get("x", expect=decoder(M))
    assert "token expired" in str(ei.value)


async def test_4xx_body_snippet_is_bounded_and_single_line(httpx_mock: HTTPXMock) -> None:
    """The folded body is a bounded, whitespace-collapsed snippet — a 4xx HTML
    error page must not turn the message into a payload."""

    import msgspec

    from headgate.errors import UpstreamStatusError

    class M(msgspec.Struct):
        a: int

    httpx_mock.add_response(
        url="https://api.example/x",
        status_code=400,
        content=b"<html>\n" + b"x" * 5000 + b"\nTAIL-MARKER</html>",
    )
    client = _StubClient(rate=Rate.per_second(2), base_url="https://api.example")
    with pytest.raises(UpstreamStatusError) as ei:
        await client.get("x", expect=decoder(M))
    msg = str(ei.value)
    assert "TAIL-MARKER" not in msg
    assert len(msg) < 500
    assert "\n" not in msg


# ---------- auth via httpx2.Auth (the _request_auth_headers hook is gone) ----------


async def test_no_auth_no_authorization_header(httpx_mock: HTTPXMock) -> None:
    """A client built without ``auth=`` sends no Authorization header."""
    httpx_mock.add_response(url="https://api.example/x", json={})
    client = _StubClient(rate=Rate.per_second(2), base_url="https://api.example")
    await client.get("x", expect=RESPONSE)
    req = httpx_mock.get_requests()[0]
    assert "Authorization" not in req.headers


async def test_explicit_headers_win_over_auth(httpx_mock: HTTPXMock) -> None:
    """An explicit ``headers=`` value wins a clash against the Auth flow
    (setdefault precedence — the invariant the hook merge used to guarantee)."""
    httpx_mock.add_response(url="https://api.example/x", json={})
    client = _StubClient(
        rate=Rate.per_second(2),
        base_url="https://api.example",
        auth=StaticHeaderAuth({"Authorization": "Bearer T"}),
    )
    await client.get("x", expect=RESPONSE, headers={"Authorization": "X"})
    req = httpx_mock.get_requests()[0]
    assert req.headers.get("Authorization") == "X"


# ---------- auth= param: httpx2.Auth threaded into AsyncClient ----------


async def test_auth_param_threaded_to_async_client(httpx_mock: HTTPXMock) -> None:
    """A ``httpx2.Auth`` passed as ``auth=`` runs at send time and stamps the
    request — proving the base threads it into the underlying AsyncClient."""

    class _StampAuth(httpx2.Auth):
        @override
        def auth_flow(self, request: httpx2.Request) -> Generator[httpx2.Request, httpx2.Response]:
            request.headers.setdefault("X-A", "1")
            yield request

    httpx_mock.add_response(url="https://api.example/x", json={})
    client = _StubClient(rate=Rate.per_second(2), base_url="https://api.example", auth=_StampAuth())
    await client.get("x", expect=RESPONSE)
    req = httpx_mock.get_requests()[0]
    assert req.headers["X-A"] == "1"


# ---------- AsyncBaseClient is an AbstractAsyncContextManager ----------


def test_async_base_client_is_abstract_async_context_manager() -> None:
    """AsyncBaseClient inherits the stdlib protocol — ``async with``
    works without us writing the methods, and isinstance checks pass."""
    from contextlib import AbstractAsyncContextManager

    client = _StubClient(rate=Rate.per_second(2), base_url="https://a.example")
    assert isinstance(client, AbstractAsyncContextManager)


# ---------- get: msgspec strict decode is a _responses.decoder knob ----------


async def test_struct_decode_lax_optin_coerces_strings() -> None:
    """strict=False (an explicit opt-out — the default is now strict) keeps the
    example-candles contract: string-quoted numbers on the wire coerce into
    number-typed struct fields."""
    import msgspec

    class _Px(msgspec.Struct):
        price: float | None = None

    def handler(req: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, content=b'{"price":"59695.24"}')

    client = _StubClient(
        rate=Rate.per_second(2), base_url="https://api.example", transport=httpx2.MockTransport(handler)
    )
    out = await client.get("x", expect=decoder(_Px, strict=False))
    assert out is not None and out.price == 59695.24


async def test_struct_decode_strict_rejects_string_quoted_numbers() -> None:
    """strict=True makes number-typed wires exact-or-fail: a string-quoted
    number raises ResponseParseError instead of coercing."""
    import msgspec

    from headgate.errors import ResponseParseError

    class _Px(msgspec.Struct):
        price: float | None = None

    def handler(req: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, content=b'{"price":"59695.24"}')

    client = _StubClient(
        rate=Rate.per_second(2),
        base_url="https://api.example",
        transport=httpx2.MockTransport(handler),
    )
    with pytest.raises(ResponseParseError):
        await client.get("x", expect=decoder(_Px, strict=True))


async def test_struct_decode_strict_accepts_integer_tokens_for_float_fields() -> None:
    """strict=True still decodes JSON *integer* tokens into float fields —
    a number is a number (Example sends open_interest as an int token)."""
    import msgspec

    class _Px(msgspec.Struct):
        oi: float | None = None

    def handler(req: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, content=b'{"oi":837345900}')

    client = _StubClient(
        rate=Rate.per_second(2), base_url="https://api.example", transport=httpx2.MockTransport(handler)
    )
    out = await client.get("x", expect=decoder(_Px, strict=True))
    assert out is not None and out.oi == 837345900.0


# ---------- refusal: the host's own check, ahead of everything the call declared ----------


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
        refusal=_key_page_refusal,
    )
    with pytest.raises(AuthError) as ei:
        await client.get("x", expect=decoder(dict[str, str]))
    assert ei.value.status_code == 302
    assert ei.value.code == "missing_key"
    assert ei.value.source == "_StubClient"
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
        httpx2.Response(200, content=b'{"ok":"yes"}'), httpx2.Response(404), follow_redirects=False, refusal=refusal
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
    non-empty because the reader ran at all. (A reader that merely returns a value, as an earlier version of this
    test used, cannot tell the two orderings apart — the host's ``AuthError`` propagates either way.)
    """
    seen: list[int] = []

    def declared_reader(resp: httpx2.Response) -> _Answer:
        seen.append(resp.status_code)
        raise Refused("a different refusal, if this ever runs", kind=RateLimitError)

    def refusal(resp: httpx2.Response) -> Refused | None:
        return Refused("host refused", kind=AuthError) if resp.status_code == 400 else None

    client = _serving(httpx2.Response(400, json={"message": "refused"}), follow_redirects=False, refusal=refusal)
    with pytest.raises(AuthError) as ei:
        await client.post("x", expect=Expect(decoder(_Value)).on(400, declared_reader))
    assert ei.value.status_code == 400

    assert seen == []


def test_a_refusing_client_that_follows_redirects_is_refused_at_construction() -> None:
    """The check sees only the final response, so a refusal on a followed hop would be skipped (spec §3)."""
    with pytest.raises(ValueError, match="follow_redirects=False"):
        _StubClient(rate=Rate.per_second(2), base_url="https://api.example", refusal=_key_page_refusal)


async def test_a_per_call_follow_redirects_on_a_refusing_client_is_refused() -> None:
    client = _serving(httpx2.Response(200, content=b"{}"), follow_redirects=False, refusal=_key_page_refusal)
    with pytest.raises(ValueError, match="follow redirects"):
        await client.get("x", expect=decoder(dict[str, str]), follow_redirects=True)


# ---------- declared responses: the pipeline ----------


class _Answer(msgspec.Struct, frozen=True):
    message: str


class _Value(msgspec.Struct, frozen=True):
    x: int


class _Status(msgspec.Struct, frozen=True):
    status: str


def _serving(*responses: httpx2.Response, follow_redirects: bool = True, refusal: Refusal | None = None) -> _StubClient:
    it = iter(responses)
    return _StubClient(
        rate=Rate.per_second(2),
        base_url="https://api.example",
        transport=httpx2.MockTransport(lambda req: next(it)),
        follow_redirects=follow_redirects,
        refusal=refusal,
    )


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
    """A refusal written in a 200 body carries 200 — never an invented status (spec §2 rule 4)."""

    def check(env: _Status) -> Refused | None:
        return Refused("quota spent", code=env.status, kind=RateLimitError) if env.status != "OK" else None

    client = _serving(httpx2.Response(200, json={"status": "REQUEST_NOT_PROCESSED"}))
    with pytest.raises(RateLimitError) as ei:
        await client.get("x", expect=decoder(_Status, check=check))
    assert (ei.value.status_code, ei.value.code, ei.value.source) == (200, "REQUEST_NOT_PROCESSED", "_StubClient")
    assert "quota spent" in str(ei.value)


async def test_a_refusal_from_a_declared_non_2xx_reader_keeps_that_status() -> None:
    def refusing(resp: httpx2.Response) -> _Answer:
        raise Refused("the provider refused, in its own words", code="E42")

    client = _serving(httpx2.Response(409, content=b"{}"))
    with pytest.raises(UpstreamStatusError) as ei:
        await client.post("x", expect=Expect(decoder(_Value)).on(409, refusing))
    assert (ei.value.status_code, ei.value.code) == (409, "E42")


async def test_a_status_error_never_carries_a_query_string_secret() -> None:
    """httpx2's own ``raise_for_status()`` puts the full URL, api_key included, in its message.

    some providers authenticate by query parameter, and a ``ClientError`` message reaches the application boundary's
    502 detail, so the ladder builds its own message from ``sanitize_url``. "Simplifying" ``status_error`` to
    ``resp.raise_for_status()`` must fail here.
    """
    client = _serving(httpx2.Response(404, content=b"gone"))
    with pytest.raises(UpstreamStatusError) as ei:
        await client.get("series?series_id=GDP&api_key=SECRET123", expect=as_bytes)
    assert "SECRET123" not in str(ei.value)
    assert "api_key=%2A%2A%2A" in str(ei.value)


async def test_a_refused_reader_never_carries_a_query_string_secret() -> None:
    """Mirrors the ladder's own secret-redaction test above: ``refusal_error`` builds its message from
    ``sanitize_url`` too, not from the raw URL."""

    def refusing(resp: httpx2.Response) -> _Value:
        raise Refused("the provider refused")

    client = _serving(httpx2.Response(200, content=b"{}"))
    with pytest.raises(UpstreamStatusError) as ei:
        await client.get("series?series_id=GDP&api_key=SECRET123", expect=refusing)
    assert "SECRET123" not in str(ei.value)
    assert "api_key=%2A%2A%2A" in str(ei.value)


async def test_a_declared_403_in_an_unrecognised_shape_raises_auth_error() -> None:
    """The fall-through goes through the real ladder (``status_error``), not a bare ``UpstreamStatusError`` —
    a declared 403 that doesn't decode still raises ``AuthError``, the 401/403 branch of the same ladder the
    default layer uses (spec §3 step 4)."""
    client = _serving(httpx2.Response(403, content=b"<html>denied</html>"))
    with pytest.raises(AuthError) as ei:
        await client.post("x", expect=Expect(decoder(_Value)).on(403, decoder(_Answer)))
    assert ei.value.status_code == 403


# ---------- Constructor arguments pruned ----------


@pytest.mark.parametrize(
    "dead",
    [
        "http2",
        "max_connections",
        "max_keepalive_connections",
        "max_rate",
        "rate_limiter",
        "rate_period",
        "cache_policy",
        # A per-call ``cache_ttl=`` is on the VERBS (they all take it); this is the constructor, whose ``cache_ttl``
        # went into ``HttpCache(ttl=)``.
        "cache_ttl",
        "storage",
    ],
)
def test_the_pruned_constructor_arguments_are_gone(dead: str) -> None:
    """No client passed any of these (spec §4). A custom pool is a custom ``transport=``."""
    import inspect

    assert dead not in inspect.signature(AsyncBaseClient.__init__).parameters


def test_rate_is_a_required_keyword_only_constructor_argument() -> None:
    """A client that states no rate is a ``TypeError``, not a budget nobody chose (spec §4.1)."""
    import inspect

    rate = inspect.signature(AsyncBaseClient.__init__).parameters["rate"]

    assert rate.kind is inspect.Parameter.KEYWORD_ONLY
    assert rate.default is inspect.Parameter.empty
