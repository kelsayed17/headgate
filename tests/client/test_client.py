# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""``AsyncBaseClient``: construction, the transport stack, retries, the breaker, requests, auth and responses.

The declared-response pipeline (host check, declared answers, the status ladder) is pinned at the end.
"""

from __future__ import annotations

import inspect
import json
from contextlib import AbstractAsyncContextManager
from decimal import Decimal
from typing import TYPE_CHECKING, TypedDict, override

import httpx2
import msgspec
import pytest
from pydantic import BaseModel, ConfigDict, Field

from headgate import CachedTokenAuth, HttpOptions, Minted, Timeouts, TransportPolicy
from headgate import pydantic as pydantic_flavor
from headgate._auth import AUTH_HOP, StaticHeaderAuth
from headgate._client import AsyncBaseClient
from headgate._responses import Expect, RefusalCheck, RefusedError, as_bytes, nothing
from headgate.errors import AuthError, RateLimitError, ResponseParseError, TransportError, UpstreamStatusError
from headgate.msgspec import decoder
from headgate.ratelimit import UNLIMITED, Rate
from headgate.ratelimit._budget import Pacer
from headgate.retry import Backoff, RetryPolicy
from headgate.testing import FakeTransport, TestClock
from tests.support.breakers import failures
from tests.support.clients import StubClient
from tests.support.responses import RESPONSE

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable, Generator

    from pytest_httpx2 import HTTPXMock

    from headgate._core import CallContext

_RATE = Rate.per_second(2)

#: A 4xx error message is a bounded snippet plus a short prefix; a 5000-byte page must come out well under this.
_MESSAGE_BOUND = 500


# --- The transport stack ---


async def test_every_retry_is_paced_by_the_rate_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that retry wraps the rate budget: each of the three attempts waits its own turn."""
    waits: list[None] = []

    async def counting_wait(_budget: Pacer, _context: CallContext) -> None:
        waits.append(None)

    monkeypatch.setattr(Pacer, "wait", counting_wait)
    statuses = iter([httpx2.codes.SERVICE_UNAVAILABLE, httpx2.codes.SERVICE_UNAVAILABLE, httpx2.codes.OK])
    sent: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        sent.append(request)
        return httpx2.Response(next(statuses))

    max_attempts = 3
    client = StubClient(
        rate=_RATE,
        base_url="https://x",
        policy=TransportPolicy(retry=RetryPolicy(attempts=max_attempts)),
        http=HttpOptions(transport=httpx2.MockTransport(handler)),
    )
    assert (await client.get("y", expect=RESPONSE)).status_code == httpx2.codes.OK
    assert len(sent) == len(waits) == max_attempts


async def test_a_client_reports_its_failures_to_the_shared_breaker(httpx2_mock: HTTPXMock) -> None:
    """Check that a failed call is charged to the process-wide breaker of the host it addressed."""
    httpx2_mock.add_response(url="https://api.example/x", status_code=httpx2.codes.SERVICE_UNAVAILABLE)
    client = StubClient(
        rate=_RATE, base_url="https://api.example", policy=TransportPolicy(retry=RetryPolicy(attempts=1))
    )
    with pytest.raises(UpstreamStatusError):
        await client.get("x", expect=RESPONSE)
    assert failures("api.example") == 1


# --- Construction ---


def test_base_url_is_required_and_rate_defaults_to_unlimited() -> None:
    """Check that a client must state its base URL, and paces nothing itself unless it states a rate."""
    parameters = inspect.signature(AsyncBaseClient.__init__).parameters
    assert {parameters[name].kind for name in ("base_url", "rate")} == {inspect.Parameter.KEYWORD_ONLY}
    assert parameters["base_url"].default is inspect.Parameter.empty
    assert parameters["rate"].default is UNLIMITED


async def test_each_client_has_its_own_http_client() -> None:
    """Check that two clients join onto their own base URLs, and closing one leaves the other working."""
    sent: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        sent.append(str(request.url))
        return httpx2.Response(httpx2.codes.OK)

    a = StubClient(rate=_RATE, base_url="https://a.example", http=HttpOptions(transport=httpx2.MockTransport(handler)))
    b = StubClient(rate=_RATE, base_url="https://b.example", http=HttpOptions(transport=httpx2.MockTransport(handler)))
    await a.get("x", expect=nothing)
    await a.aclose()
    await b.get("x", expect=nothing)
    assert sent == ["https://a.example/x", "https://b.example/x"]


@pytest.mark.parametrize(
    ("build", "match"),
    [
        (lambda: TransportPolicy(retry=RetryPolicy(attempts=0)), "attempts"),
        (lambda: HttpOptions(timeout=Timeouts(read=0)), "read"),
        (lambda: HttpOptions(max_response_bytes=0), "max_response_bytes"),
    ],
)
def test_an_option_group_refuses_a_nonsense_value(build: Callable[[], object], match: str) -> None:
    """Check that each option group fails where it is written, not at the first request."""
    with pytest.raises(ValueError, match=match):
        build()


def test_init_config_not_exposed_on_self() -> None:
    """Check that construction arguments configure the stack and none is kept as an attribute."""
    client = StubClient(base_url="https://x", rate=Rate(requests=99, period=2.0))
    assert not hasattr(client, "base_url")
    assert not hasattr(client, "rate")


async def test_the_client_is_an_async_context_manager_that_closes_on_exit() -> None:
    """Check that the client satisfies the stdlib protocol and that leaving the block closes it."""
    client = StubClient(rate=_RATE, base_url="https://a.example")
    assert isinstance(client, AbstractAsyncContextManager)
    async with client as entered:
        assert entered is client
    with pytest.raises(RuntimeError, match="closed"):
        await client.get("x", expect=nothing)


# --- URL joining (delegated to httpx2) ---


async def test_base_url_joins_relative_path(httpx2_mock: HTTPXMock) -> None:
    """Check that a relative path joins onto a base URL that has no trailing slash."""
    httpx2_mock.add_response(url="https://api.example/v1/foo", json={"ok": True})
    client = StubClient(rate=_RATE, base_url="https://api.example/v1")
    resp = await client.get("foo", expect=RESPONSE)
    assert str(resp.url) == "https://api.example/v1/foo"


async def test_absolute_url_passes_through(httpx2_mock: HTTPXMock) -> None:
    """Check that an absolute ``str`` URL is sent as given, not joined onto the base URL."""
    httpx2_mock.add_response(url="https://other.example/raw", json={"ok": True})
    client = StubClient(rate=_RATE, base_url="https://api.example/v1")
    resp = await client.get("https://other.example/raw", expect=RESPONSE)
    assert str(resp.url) == "https://other.example/raw"


# --- Query parameters ---


class _SeriesQuery(TypedDict):
    """A client's query parameters as a TypedDict, which ``params=`` must accept."""

    series_id: str
    limit: int


async def test_query_params_are_encoded_as_httpx2_encodes_a_mapping() -> None:
    """Check that lists repeat their key, ``None`` is empty, bools are lowercase and other values are ``str()``."""
    seen: list[bytes] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request.url.query)
        return httpx2.Response(httpx2.codes.OK)

    client = StubClient(
        rate=_RATE, base_url="https://api.example", http=HttpOptions(transport=httpx2.MockTransport(handler))
    )
    params = {"a": ["1", 2], "b": None, "c": True, "d": Decimal("1.5"), "e": ("x",)}
    await client.get("x", expect=nothing, params=params)
    query: _SeriesQuery = {"series_id": "GDP", "limit": 3}
    await client.get("x", expect=nothing, params=query)
    assert seen == [b"a=1&a=2&b=&c=true&d=1.5&e=x", b"series_id=GDP&limit=3"]


# --- Retry on transient status ---


async def test_retries_on_503_then_succeeds(httpx2_mock: HTTPXMock) -> None:
    """Check that two 503s are retried past to the 200 behind them."""
    httpx2_mock.add_response(url="https://api.example/x", status_code=httpx2.codes.SERVICE_UNAVAILABLE)
    httpx2_mock.add_response(url="https://api.example/x", status_code=httpx2.codes.SERVICE_UNAVAILABLE)
    httpx2_mock.add_response(url="https://api.example/x", json={"ok": True})
    client = StubClient(
        rate=_RATE, base_url="https://api.example", policy=TransportPolicy(retry=RetryPolicy(attempts=3))
    )
    resp = await client.get("x", expect=RESPONSE)
    assert resp.status_code == httpx2.codes.OK


async def test_raises_after_exhausting_retries(httpx2_mock: HTTPXMock) -> None:
    """Check that a 5xx surviving every attempt raises."""
    for _ in range(3):
        httpx2_mock.add_response(url="https://api.example/x", status_code=httpx2.codes.SERVICE_UNAVAILABLE)
    client = StubClient(
        rate=_RATE, base_url="https://api.example", policy=TransportPolicy(retry=RetryPolicy(attempts=3))
    )
    with pytest.raises(UpstreamStatusError):
        await client.get("x", expect=RESPONSE)


async def test_honors_retry_after_on_429(httpx2_mock: HTTPXMock) -> None:
    """Check that a 429's ``Retry-After`` is waited exactly, not the backoff curve and not clamped to its cap.

    The client's sleep records the duration instead of waiting it, so asserting the duration costs no wall time.
    """
    retry_after = 7.0
    httpx2_mock.add_response(
        url="https://api.example/x",
        status_code=httpx2.codes.TOO_MANY_REQUESTS,
        headers={"Retry-After": f"{retry_after:g}"},
    )
    httpx2_mock.add_response(url="https://api.example/x", json={"ok": True})

    # A zero backoff factor, so the backoff fallback would be a visible 0.0, not a plausible-looking wait.
    policy = TransportPolicy(retry=RetryPolicy(attempts=2, backoff=Backoff(factor=0.0)))
    clock = TestClock()
    with clock.install():
        client = StubClient(rate=_RATE, base_url="https://api.example", policy=policy)
    resp = await client.get("x", expect=RESPONSE)

    assert resp.status_code == httpx2.codes.OK
    assert clock.sleeps == [retry_after], "the header's 7s is waited exactly, past the backoff's max_wait of 5s"


# --- Circuit breaker ---


async def test_circuit_breaker_opens_after_threshold(httpx2_mock: HTTPXMock) -> None:
    """Check that five consecutive failures open the host's breaker, which then refuses without sending."""
    client = StubClient(
        rate=_RATE, base_url="https://api.example", policy=TransportPolicy(retry=RetryPolicy(attempts=1))
    )
    for _ in range(5):
        httpx2_mock.add_response(url="https://api.example/x", status_code=httpx2.codes.SERVICE_UNAVAILABLE)
        with pytest.raises(UpstreamStatusError):
            await client.get("x", expect=RESPONSE)
    pre_request_count = len(httpx2_mock.get_requests())
    with pytest.raises(TransportError):
        await client.get("x", expect=RESPONSE)
    assert len(httpx2_mock.get_requests()) == pre_request_count


async def test_circuit_breaker_resets_on_success(httpx2_mock: HTTPXMock) -> None:
    """Check that a success after a failure resets the host's failure count."""
    httpx2_mock.add_response(url="https://api.example/x", status_code=httpx2.codes.SERVICE_UNAVAILABLE)
    client = StubClient(
        rate=_RATE, base_url="https://api.example", policy=TransportPolicy(retry=RetryPolicy(attempts=1))
    )
    with pytest.raises(UpstreamStatusError):
        await client.get("x", expect=RESPONSE)
    httpx2_mock.add_response(url="https://api.example/x", json={"ok": True})
    await client.get("x", expect=RESPONSE)
    assert failures("api.example") == 0


class _HopAuth(CachedTokenAuth):
    """Mint a token through the owning client's stack, with one hop to a separate credential host."""

    @override
    async def mint_flow(self, request: httpx2.Request) -> AsyncGenerator[httpx2.Request | Minted[str], httpx2.Response]:
        del request  # the hop is its own request
        response = yield httpx2.Request("POST", "https://auth.example/token", extensions={AUTH_HOP: True})
        yield Minted(response.text, 3600.0)


async def test_a_failed_auth_hop_is_not_charged_to_the_called_host() -> None:
    """Check that a credential host being unreachable says nothing about the host the call addressed."""

    def handler(request: httpx2.Request) -> httpx2.Response:
        msg = "credential host down"
        raise httpx2.ConnectError(msg, request=request)

    client = StubClient(
        rate=_RATE,
        base_url="https://api.example",
        auth=_HopAuth(),
        policy=TransportPolicy(retry=RetryPolicy(attempts=1)),
        http=HttpOptions(transport=httpx2.MockTransport(handler)),
    )
    with pytest.raises(TransportError, match="credential host down"):
        await client.get("x", expect=nothing)
    assert failures("api.example") == 0


# --- Request bodies ---


class _OrderBody(BaseModel):
    """A request body whose wire names differ from its field names."""

    model_config = ConfigDict(populate_by_name=True)
    order_side: str = Field(serialization_alias="orderSide")
    stop_price: float | None = None


def _order_json() -> dict[str, object]:
    """Return the order as the wire spells it: aliases applied, ``None`` fields dropped."""
    return pydantic_flavor.to_builtins(_OrderBody(order_side="buy"), drop_none=True)


async def test_json_body_kwarg_rides_through_with_content_type(httpx2_mock: HTTPXMock) -> None:
    """Check that ``json=`` builtins are serialized by httpx2 and stamped with the JSON content type."""
    httpx2_mock.add_response(url="https://api.example/o", json={"ok": True})
    client = StubClient(rate=_RATE, base_url="https://api.example")
    await client.post("o", expect=RESPONSE, json=_order_json())
    req = httpx2_mock.get_requests()[0]
    assert req.headers["content-type"] == "application/json"
    assert json.loads(req.content) == {"orderSide": "buy"}


async def test_json_body_content_type_is_overridable(httpx2_mock: HTTPXMock) -> None:
    """Check that an explicit caller ``Content-Type`` wins over the one ``json=`` stamps."""
    httpx2_mock.add_response(url="https://api.example/o", json={})
    client = StubClient(rate=_RATE, base_url="https://api.example")
    await client.post("o", expect=RESPONSE, json=_order_json(), headers={"Content-Type": "application/vnd.api+json"})
    req = httpx2_mock.get_requests()[0]
    assert req.headers["content-type"] == "application/vnd.api+json"


async def test_json_body_does_not_mutate_caller_headers(httpx2_mock: HTTPXMock) -> None:
    """Check that httpx2 copies the caller's headers rather than adding the content type to them."""
    httpx2_mock.add_response(url="https://api.example/o", json={})
    client = StubClient(rate=_RATE, base_url="https://api.example")
    caller_headers: dict[str, str] = {"X-Custom": "1"}
    await client.post("o", expect=RESPONSE, json=_order_json(), headers=caller_headers)
    assert caller_headers == {"X-Custom": "1"}


async def test_a_form_body_rides_data() -> None:
    """Check that ``data=`` is sent form-encoded."""
    seen: list[bytes] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request.content)
        return httpx2.Response(httpx2.codes.OK)

    client = StubClient(
        rate=_RATE, base_url="https://api.example", http=HttpOptions(transport=httpx2.MockTransport(handler))
    )
    await client.post("o", expect=nothing, data={"side": "buy"})
    assert seen == [b"side=buy"]


@pytest.mark.parametrize(
    ("per_call", "expected"),
    [
        pytest.param(Timeouts(read=5.0), {"connect": 10.0, "read": 5.0, "write": 10.0, "pool": 15.0}, id="one-phase"),
        pytest.param(
            Timeouts(connect=1.0, read=2.0, write=3.0, pool=4.0),
            {"connect": 1.0, "read": 2.0, "write": 3.0, "pool": 4.0},
            id="every-phase",
        ),
    ],
)
async def test_timeout_named_param_forwarded_to_httpx(per_call: Timeouts, expected: dict[str, float]) -> None:
    """Check that a per-call ``timeout=`` replaces the phases it sets, and the defaults fill in the rest."""
    captured: dict[str, object] = {}

    def handler(req: httpx2.Request) -> httpx2.Response:
        captured["timeout"] = req.extensions.get("timeout")
        return httpx2.Response(httpx2.codes.OK, json={})

    client = StubClient(
        rate=_RATE, base_url="https://api.example", http=HttpOptions(transport=httpx2.MockTransport(handler))
    )
    await client.get("x", expect=RESPONSE, timeout=per_call)
    assert captured["timeout"] == expected


# --- Non-transient 4xx is not retried ---


async def test_non_transient_4xx_raises_after_one_request(httpx2_mock: HTTPXMock) -> None:
    """Check that a non-transient 4xx (a 404) is not retried and raises with its real status."""
    httpx2_mock.add_response(url="https://api.example/x", status_code=httpx2.codes.NOT_FOUND)
    client = StubClient(
        rate=_RATE, base_url="https://api.example", policy=TransportPolicy(retry=RetryPolicy(attempts=3))
    )
    with pytest.raises(UpstreamStatusError) as ei:
        await client.get("x", expect=RESPONSE)
    assert ei.value.status_code == httpx2.codes.NOT_FOUND
    assert len(httpx2_mock.get_requests()) == 1


async def test_403_raises_auth_error_after_one_request(httpx2_mock: HTTPXMock) -> None:
    """Check that a 403 is not retried and raises ``AuthError``, naming this client's credential."""
    httpx2_mock.add_response(url="https://api.example/x", status_code=httpx2.codes.FORBIDDEN)
    client = StubClient(
        rate=_RATE, base_url="https://api.example", policy=TransportPolicy(retry=RetryPolicy(attempts=3))
    )
    with pytest.raises(AuthError) as ei:
        await client.get("x", expect=RESPONSE)
    assert ei.value.status_code == httpx2.codes.FORBIDDEN
    assert len(httpx2_mock.get_requests()) == 1


# --- The reported failure reflects the actual last attempt ---


async def test_reported_failure_reflects_the_last_attempt_kind(httpx2_mock: HTTPXMock) -> None:
    """Check that after a 503 and then network failures, the raised error describes the final failure.

    The property is checked on the caller-visible exception, so it cannot pass while what a caller sees is wrong.
    """
    httpx2_mock.add_response(url="https://api.example/x", status_code=httpx2.codes.SERVICE_UNAVAILABLE)
    httpx2_mock.add_exception(httpx2.ConnectError("net"))
    httpx2_mock.add_exception(httpx2.ConnectError("net"))
    client = StubClient(
        rate=_RATE, base_url="https://api.example", policy=TransportPolicy(retry=RetryPolicy(attempts=3))
    )
    with pytest.raises(TransportError) as ei:
        await client.get("x", expect=RESPONSE)

    detail = str(ei.value)
    assert "net" in detail, f"Expected ConnectError 'net' in the raised error but got: {detail!r}"
    assert "503" not in detail, f"Stale 503 leaked into the raised error: {detail!r}"
    # The request line carries the relative path the caller passed; the client name in the prefix names the host.
    assert "GET x" in detail, f"Request line missing from the error: {detail!r}"


# --- An undeclared 4xx carries its body ---


class _M(msgspec.Struct):
    """A success shape no error body matches."""

    a: int


async def test_an_undeclared_4xx_folds_its_body_into_the_message(httpx2_mock: HTTPXMock) -> None:
    """Check that an undeclared 4xx puts the upstream's own reason, the diagnosis, into the error message."""
    httpx2_mock.add_response(
        url="https://api.example/x",
        status_code=httpx2.codes.BAD_REQUEST,
        content=b'{"code":3003,"message":"insufficient balance to cover this order"}',
    )
    client = StubClient(rate=_RATE, base_url="https://api.example")
    with pytest.raises(UpstreamStatusError) as ei:
        await client.get("x", expect=decoder(_M))
    assert "insufficient balance to cover this order" in str(ei.value)


async def test_an_undeclared_403_folds_its_body_into_the_message(httpx2_mock: HTTPXMock) -> None:
    """Check that the 401/403 branch carries the body too: it says why the credential was refused."""
    httpx2_mock.add_response(
        url="https://api.example/x", status_code=httpx2.codes.FORBIDDEN, content=b'{"message":"token expired"}'
    )
    client = StubClient(rate=_RATE, base_url="https://api.example")
    with pytest.raises(AuthError) as ei:
        await client.get("x", expect=decoder(_M))
    assert "token expired" in str(ei.value)


async def test_4xx_body_snippet_is_bounded_and_single_line(httpx2_mock: HTTPXMock) -> None:
    """Check that the folded body is bounded and whitespace-collapsed, so an HTML page cannot become a payload."""
    httpx2_mock.add_response(
        url="https://api.example/x",
        status_code=httpx2.codes.BAD_REQUEST,
        content=b"<html>\n" + b"x" * 5000 + b"\nTAIL-MARKER</html>",
    )
    client = StubClient(rate=_RATE, base_url="https://api.example")
    with pytest.raises(UpstreamStatusError) as ei:
        await client.get("x", expect=decoder(_M))
    msg = str(ei.value)
    assert "TAIL-MARKER" not in msg
    assert len(msg) < _MESSAGE_BOUND
    assert "\n" not in msg


async def test_a_blank_error_body_adds_no_snippet(httpx2_mock: HTTPXMock) -> None:
    """Check that a whitespace-only error body leaves the error without a body snippet."""
    httpx2_mock.add_response(url="https://api.example/x", status_code=httpx2.codes.BAD_REQUEST, content=b" \n\t ")
    client = StubClient(rate=_RATE, base_url="https://api.example")
    with pytest.raises(UpstreamStatusError) as ei:
        await client.get("x", expect=decoder(_M))
    assert str(ei.value).endswith(f"(status={httpx2.codes.BAD_REQUEST})")


# --- Auth ---


async def test_no_auth_no_authorization_header(httpx2_mock: HTTPXMock) -> None:
    """Check that a client built without ``auth=`` sends no Authorization header."""
    httpx2_mock.add_response(url="https://api.example/x", json={})
    client = StubClient(rate=_RATE, base_url="https://api.example")
    await client.get("x", expect=RESPONSE)
    req = httpx2_mock.get_requests()[0]
    assert "Authorization" not in req.headers


async def test_explicit_headers_win_over_auth(httpx2_mock: HTTPXMock) -> None:
    """Check that an explicit ``headers=`` value wins a clash with the auth flow (``StaticHeaderAuth`` setdefaults)."""
    httpx2_mock.add_response(url="https://api.example/x", json={})
    client = StubClient(
        rate=_RATE, base_url="https://api.example", auth=StaticHeaderAuth({"Authorization": "Bearer T"})
    )
    await client.get("x", expect=RESPONSE, headers={"Authorization": "X"})
    req = httpx2_mock.get_requests()[0]
    assert req.headers.get("Authorization") == "X"


class _StampAuth(httpx2.Auth):
    """Stamp one header onto every request."""

    @override
    def auth_flow(self, request: httpx2.Request) -> Generator[httpx2.Request, httpx2.Response]:
        request.headers.setdefault("X-A", "1")
        yield request


async def test_auth_param_threaded_to_async_client(httpx2_mock: HTTPXMock) -> None:
    """Check that an ``httpx2.Auth`` passed as ``auth=`` runs at send time and stamps the request."""
    httpx2_mock.add_response(url="https://api.example/x", json={})
    client = StubClient(rate=_RATE, base_url="https://api.example", auth=_StampAuth())
    await client.get("x", expect=RESPONSE)
    req = httpx2_mock.get_requests()[0]
    assert req.headers["X-A"] == "1"


# --- msgspec strictness: decoder(strict=) ---

_QUOTED_PRICE = "59695.24"
_OPEN_INTEREST = 837345900


class _Px(msgspec.Struct):
    """A quote whose price is number-typed."""

    price: float | None = None


class _Oi(msgspec.Struct):
    """Open interest, number-typed."""

    oi: float | None = None


def _replying(content: bytes) -> StubClient:
    """Return a client whose every request is answered 200 with *content*."""
    transport = httpx2.MockTransport(lambda _request: httpx2.Response(httpx2.codes.OK, content=content))
    return StubClient(rate=_RATE, base_url="https://api.example", http=HttpOptions(transport=transport))


async def test_struct_decode_lax_opt_in_coerces_strings() -> None:
    """Check that ``strict=False`` (an explicit opt-out) coerces a string-quoted number into a number field."""
    client = _replying(b'{"price":"%s"}' % _QUOTED_PRICE.encode())
    out = await client.get("x", expect=decoder(_Px, strict=False))
    assert out.price == float(_QUOTED_PRICE)


async def test_struct_decode_strict_rejects_string_quoted_numbers() -> None:
    """Check that ``strict=True`` makes a string-quoted number a ``ResponseParseError`` instead of coercing it."""
    client = _replying(b'{"price":"%s"}' % _QUOTED_PRICE.encode())
    with pytest.raises(ResponseParseError):
        await client.get("x", expect=decoder(_Px, strict=True))


async def test_struct_decode_strict_accepts_integer_tokens_for_float_fields() -> None:
    """Check that ``strict=True`` still decodes a JSON integer into a float field: a number is a number."""
    client = _replying(b'{"oi":%d}' % _OPEN_INTEREST)
    out = await client.get("x", expect=decoder(_Oi, strict=True))
    assert out.oi == float(_OPEN_INTEREST)


# --- Declared-response helpers ---


class _Answer(msgspec.Struct, frozen=True):
    """A documented non-2xx answer."""

    message: str


class _Value(msgspec.Struct, frozen=True):
    """A success body."""

    x: int


class _Status(msgspec.Struct, frozen=True):
    """An envelope whose ``status`` may carry a refusal inside a 200."""

    status: str


def _serving(
    *responses: httpx2.Response, follow_redirects: bool = True, refusal_check: RefusalCheck | None = None
) -> StubClient:
    """Return a client whose successive requests are answered by *responses*, in order."""
    it = iter(responses)
    return StubClient(
        rate=_RATE,
        base_url="https://api.example",
        http=HttpOptions(
            transport=httpx2.MockTransport(lambda _request: next(it)),
            follow_redirects=follow_redirects,
            refusal_check=refusal_check,
        ),
    )


# --- Refusal: the host's own check, ahead of everything the call declared ---


def _key_page_refusal(resp: httpx2.Response) -> RefusedError | None:
    if resp.is_redirect and resp.headers.get("location", "").endswith("missing_key.html"):
        return RefusedError("rejected credential", kind=AuthError, code="missing_key")
    return None


async def test_a_host_refusal_is_raised_as_its_kind_with_the_real_status() -> None:
    """Check that a key-error 302 named by the host check raises with the 302, the code, the source and the body."""
    client = _serving(
        httpx2.Response(
            httpx2.codes.FOUND,
            headers={"Location": "https://api.example/missing_key.html"},
            content=b"Moved to key page",
        ),
        follow_redirects=False,
        refusal_check=_key_page_refusal,
    )
    with pytest.raises(AuthError) as ei:
        await client.get("x", expect=decoder(dict[str, str]))
    assert ei.value.status_code == httpx2.codes.FOUND
    assert ei.value.code == "missing_key"
    assert ei.value.source == "StubClient"
    assert "Moved to key page" in str(ei.value)


async def test_a_host_check_returning_none_changes_nothing_and_sees_every_response() -> None:
    """Check that a host check returning ``None`` lets a 2xx decode and a non-2xx raise, and sees both."""
    seen: list[int] = []

    def refusal(resp: httpx2.Response) -> RefusedError | None:
        seen.append(resp.status_code)
        return None

    client = _serving(
        httpx2.Response(httpx2.codes.OK, content=b'{"ok":"yes"}'),
        httpx2.Response(httpx2.codes.NOT_FOUND),
        follow_redirects=False,
        refusal_check=refusal,
    )
    assert await client.get("x", expect=decoder(dict[str, str])) == {"ok": "yes"}
    with pytest.raises(UpstreamStatusError) as ei:
        await client.get("x", expect=decoder(dict[str, str]))
    assert ei.value.status_code == httpx2.codes.NOT_FOUND
    assert seen == [httpx2.codes.OK, httpx2.codes.NOT_FOUND]


async def test_the_host_check_runs_before_a_declared_answer() -> None:
    """Check that a host refusal wins even on a status the call declared.

    The declared reader for 400 raises a different refusal (``RateLimitError``) and records every status it reads, so
    running the endpoint step first would show twice: as the wrong type, and as a non-empty ``seen``.
    """
    seen: list[int] = []

    def declared_reader(resp: httpx2.Response) -> _Answer:
        seen.append(resp.status_code)
        msg = "a different refusal, if this ever runs"
        raise RefusedError(msg, kind=RateLimitError)

    def refusal(resp: httpx2.Response) -> RefusedError | None:
        return RefusedError("host refused", kind=AuthError) if resp.status_code == httpx2.codes.BAD_REQUEST else None

    client = _serving(
        httpx2.Response(httpx2.codes.BAD_REQUEST, json={"message": "refused"}),
        follow_redirects=False,
        refusal_check=refusal,
    )
    with pytest.raises(AuthError) as ei:
        await client.post("x", expect=Expect(decoder(_Value)).on(httpx2.codes.BAD_REQUEST, declared_reader))
    assert ei.value.status_code == httpx2.codes.BAD_REQUEST
    assert seen == []


def test_a_refusing_client_that_follows_redirects_is_refused_at_construction() -> None:
    """Check that a refusal check on a redirect-following client is a ``ValueError``: it would miss a hop's refusal."""
    with pytest.raises(ValueError, match="follow_redirects=False"):
        HttpOptions(refusal_check=_key_page_refusal)


async def test_a_per_call_follow_redirects_on_a_refusing_client_is_refused() -> None:
    """Check that one call cannot turn redirect-following on for a client with a refusal check."""
    client = _serving(
        httpx2.Response(httpx2.codes.OK, content=b"{}"), follow_redirects=False, refusal_check=_key_page_refusal
    )
    with pytest.raises(ValueError, match="follow redirects"):
        await client.get("x", expect=decoder(dict[str, str]), follow_redirects=True)


# --- Declared responses: the pipeline ---


async def test_a_declared_non_2xx_reaches_its_reader() -> None:
    """Check that a declared 400 is read by its own reader."""
    client = _serving(httpx2.Response(httpx2.codes.BAD_REQUEST, json={"message": "refused"}))
    out = await client.post("x", expect=Expect(decoder(_Value)).on(httpx2.codes.BAD_REQUEST, decoder(_Answer)))
    assert out == _Answer(message="refused")


async def test_an_exact_2xx_declaration_wins_over_the_success_reader() -> None:
    """Check that an exactly declared 204 goes to its reader, not to the success reader."""
    client = _serving(httpx2.Response(httpx2.codes.NO_CONTENT))
    assert await client.post("x", expect=Expect(decoder(_Value)).on(httpx2.codes.NO_CONTENT, nothing)) is None


async def test_a_declared_non_2xx_in_an_unrecognized_shape_falls_through_to_the_ladder() -> None:
    """Check that a declared status in a shape its reader does not know raises with its real status."""
    client = _serving(httpx2.Response(httpx2.codes.BAD_REQUEST, content=b"<html>gateway</html>"))
    with pytest.raises(UpstreamStatusError) as ei:
        await client.post("x", expect=Expect(decoder(_Value)).on(httpx2.codes.BAD_REQUEST, decoder(_Answer)))
    assert ei.value.status_code == httpx2.codes.BAD_REQUEST
    assert type(ei.value) is UpstreamStatusError


async def test_a_2xx_that_does_not_decode_is_a_parse_error() -> None:
    """Check that an undecodable 2xx body is a ``ResponseParseError``."""
    client = _serving(httpx2.Response(httpx2.codes.OK, content=b"not json"))
    with pytest.raises(ResponseParseError):
        await client.get("x", expect=decoder(_Value))


async def test_an_undeclared_status_raises_even_beside_a_declared_one() -> None:
    """Check that declaring a 400 does not make a 404 an answer."""
    client = _serving(httpx2.Response(httpx2.codes.NOT_FOUND, json={"message": "gone"}))
    with pytest.raises(UpstreamStatusError) as ei:
        await client.post("x", expect=Expect(decoder(_Value)).on(httpx2.codes.BAD_REQUEST, decoder(_Answer)))
    assert ei.value.status_code == httpx2.codes.NOT_FOUND


async def test_nothing_returns_none_for_a_2xx_whatever_its_body() -> None:
    """Check that ``nothing`` returns ``None`` for a 2xx and ignores its body."""
    client = _serving(httpx2.Response(httpx2.codes.OK, content=b"Cancel request was submitted"))
    assert await client.delete("x", expect=nothing) is None


async def test_a_refused_reader_becomes_its_kind_with_the_real_status_and_code() -> None:
    """Check that a refusal written in a 200 body carries 200, never an invented status."""

    def check(env: _Status) -> RefusedError | None:
        return RefusedError("quota spent", code=env.status, kind=RateLimitError) if env.status != "OK" else None

    client = _serving(httpx2.Response(httpx2.codes.OK, json={"status": "REQUEST_NOT_PROCESSED"}))
    with pytest.raises(RateLimitError) as ei:
        await client.get("x", expect=decoder(_Status, refusal_check=check))
    assert (ei.value.status_code, ei.value.code, ei.value.source) == (
        httpx2.codes.OK,
        "REQUEST_NOT_PROCESSED",
        "StubClient",
    )
    assert "quota spent" in str(ei.value)


async def test_a_refusal_from_a_declared_non_2xx_reader_keeps_that_status() -> None:
    """Check that a declared 409 whose reader refuses raises with 409 and the reader's code."""

    def refusing(_resp: httpx2.Response) -> _Answer:
        msg = "the provider refused, in its own words"
        raise RefusedError(msg, code="E42")

    client = _serving(httpx2.Response(httpx2.codes.CONFLICT, content=b"{}"))
    with pytest.raises(UpstreamStatusError) as ei:
        await client.post("x", expect=Expect(decoder(_Value)).on(httpx2.codes.CONFLICT, refusing))
    assert (ei.value.status_code, ei.value.code) == (httpx2.codes.CONFLICT, "E42")


async def test_a_status_error_never_carries_a_query_string_secret() -> None:
    """Check that the status ladder's message redacts a query-string credential.

    httpx2's own ``raise_for_status()`` puts the full URL in its message, so "simplifying" ``status_error`` to it must
    fail here.
    """
    client = _serving(httpx2.Response(httpx2.codes.NOT_FOUND, content=b"gone"))
    with pytest.raises(UpstreamStatusError) as ei:
        await client.get("series?series_id=GDP&api_key=SECRET123", expect=as_bytes)
    assert "SECRET123" not in str(ei.value)
    assert "api_key=%2A%2A%2A" in str(ei.value)


async def test_a_refused_reader_never_carries_a_query_string_secret() -> None:
    """Check that ``refusal_error`` builds its message from the sanitized URL too."""

    def refusing(_resp: httpx2.Response) -> _Value:
        msg = "the provider refused"
        raise RefusedError(msg)

    client = _serving(httpx2.Response(httpx2.codes.OK, content=b"{}"))
    with pytest.raises(UpstreamStatusError) as ei:
        await client.get("series?series_id=GDP&api_key=SECRET123", expect=refusing)
    assert "SECRET123" not in str(ei.value)
    assert "api_key=%2A%2A%2A" in str(ei.value)


async def test_a_declared_403_in_an_unrecognized_shape_raises_auth_error() -> None:
    """Check that the fall-through uses the real ladder, so an undecodable declared 403 is still ``AuthError``."""
    client = _serving(httpx2.Response(httpx2.codes.FORBIDDEN, content=b"<html>denied</html>"))
    with pytest.raises(AuthError) as ei:
        await client.post("x", expect=Expect(decoder(_Value)).on(httpx2.codes.FORBIDDEN, decoder(_Answer)))
    assert ei.value.status_code == httpx2.codes.FORBIDDEN


@pytest.mark.transport_policy
async def test_a_client_without_a_rate_paces_nothing_but_obeys_a_server_pause() -> None:
    """Send back to back without ``rate=``, and wait out the pause a 429's ``Retry-After`` asks for."""
    fake = FakeTransport().reply("GET", "/x", 200, 200, httpx2.Response(429, headers={"Retry-After": "2"}), 200)
    clock = TestClock()
    with clock.install():
        client = AsyncBaseClient(base_url="https://api.example", http=HttpOptions(transport=fake))
    async with client:
        for _ in range(2):
            await client.get("x", expect=nothing)
        assert clock.sleeps == []
        with pytest.raises(RateLimitError):
            await client.get("x", expect=nothing, retry=False)
        await client.get("x", expect=nothing)
    assert clock.sleeps == [2.0]
