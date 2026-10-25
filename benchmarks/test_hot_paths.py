# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""The pure functions every request runs through, measured on their own.

Each is cheap in isolation; these benchmarks exist so that a change which makes one of them accidentally quadratic or
allocation-heavy shows up as a regression of that function rather than as noise in the end-to-end numbers.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx2
import msgspec
from pydantic import BaseModel

from headgate import msgspec as msgspec_flavor
from headgate import pydantic as pydantic_flavor
from headgate._breaker import CircuitBreaker
from headgate._failures import status_error
from headgate._rate_limit import PathLimiters, Rate, RateRule
from headgate._retry import build_retry
from headgate._url import escape_segment, redact_secrets, render_path, sanitize_url

if TYPE_CHECKING:
    from pytest_codspeed import BenchmarkFixture

_ROWS = 1000
_KEYED_URL = "https://api.example.test/v1/series/observations?series_id=GDP&api_key=0123456789abcdef&limit=100"
_ERROR_TEXT = f"GET {_KEYED_URL}: ConnectError: connection refused while sending token=abc123&page=2"


class MsgspecRow(msgspec.Struct, frozen=True):
    """One row of a list-shaped body, as a msgspec struct."""

    date: str
    value: float


class PydanticRow(BaseModel, frozen=True):
    """One row of a list-shaped body, as a pydantic model."""

    date: str
    value: float


_ROWS_BODY = msgspec.json.encode([{"date": f"2026-01-{i % 28 + 1:02d}", "value": i * 0.5} for i in range(_ROWS)])
_ROWS_RESPONSE = httpx2.Response(httpx2.codes.OK, content=_ROWS_BODY)
_MSGSPEC_ROWS = msgspec_flavor.decoder(list[MsgspecRow])
_PYDANTIC_ROWS = pydantic_flavor.decoder(list[PydanticRow])


def test_escape_segment(benchmark: BenchmarkFixture) -> None:
    """Measure escaping one path segment that needs percent-encoding."""
    benchmark(escape_segment, "BTC/USD:spot")


def test_render_path(benchmark: BenchmarkFixture) -> None:
    """Measure rendering a t-string path with three interpolations and a nested prefix."""
    prefix = t"/api/v3/brokerage"
    account, symbol, side = "a1b2c3", "BTC/USD", "buy"
    benchmark(lambda: render_path(t"{prefix}/accounts/{account}/orders/{symbol}/{side}"))


def test_sanitize_url(benchmark: BenchmarkFixture) -> None:
    """Measure redacting a credential from a URL with a query string."""
    benchmark(sanitize_url, _KEYED_URL)


def test_redact_secrets(benchmark: BenchmarkFixture) -> None:
    """Measure redacting credentials from a transport error message."""
    benchmark(redact_secrets, _ERROR_TEXT)


def test_status_error(benchmark: BenchmarkFixture) -> None:
    """Measure building the ``ClientError`` for an undeclared 4xx, including its body snippet."""
    response = httpx2.Response(httpx2.codes.BAD_REQUEST, text="field 'limit' must be <= 1000 " * 40)
    benchmark(status_error, response, "https://api.example.test/v1/series", source="ExampleClient")


def test_msgspec_decode_rows(benchmark: BenchmarkFixture) -> None:
    """Measure decoding a 1000-row list body with the msgspec flavor."""
    benchmark(_MSGSPEC_ROWS, _ROWS_RESPONSE)


def test_pydantic_decode_rows(benchmark: BenchmarkFixture) -> None:
    """Measure decoding the same 1000-row list body with the pydantic flavor."""
    benchmark(_PYDANTIC_ROWS, _ROWS_RESPONSE)


def test_breaker_track(benchmark: BenchmarkFixture) -> None:
    """Measure one successful fetch through a closed circuit breaker."""
    breaker = CircuitBreaker(name="api.example.test")

    def fetch() -> None:
        with breaker.track():
            pass

    benchmark(fetch)


def test_retry_backoff(benchmark: BenchmarkFixture) -> None:
    """Measure computing the wait after a 503 that carries a ``Retry-After`` HTTP-date."""
    policy = build_retry(3).increment()
    failure = httpx2.Response(
        httpx2.codes.SERVICE_UNAVAILABLE, headers={"Retry-After": "Wed, 21 Oct 2099 07:28:00 GMT"}
    )
    benchmark(policy.sleep_seconds, failure)


def test_path_limiter_lookup(benchmark: BenchmarkFixture) -> None:
    """Measure choosing a limiter by URL path among ten rules, where the last rule matches."""
    rules = [RateRule(f"/endpoint-{i}", Rate.per_second(10)) for i in range(10)]
    limiters = PathLimiters(default=Rate.per_second(100), rules=rules)
    request = httpx2.Request("GET", "https://api.example.test/v1/endpoint-9/items")
    benchmark(limiters.for_request, request)
