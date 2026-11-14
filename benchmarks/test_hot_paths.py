# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""The pure functions every request runs through, measured on their own.

Each is cheap in isolation; these benchmarks exist so that a change which makes one of them accidentally quadratic or
allocation-heavy shows up as a regression of that function rather than as noise in the end-to-end numbers.

CodSpeed's CPU simulation measures a single call of the benchmarked function. For a function that takes a few
microseconds, one call is mostly the cost of starting cold, which depends on what the benchmarks before it left behind,
so it moved by a sixth when only an earlier benchmark changed. A benchmark of such a function therefore measures a batch
of :data:`_CALLS` calls (named ``..._batch``), whose cost is the function's own. The row decodes and the long-word
redaction take long enough to be measured one call at a time.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx2
import msgspec
import pytest
from pydantic import BaseModel

from headgate import msgspec as msgspec_flavor
from headgate import pydantic as pydantic_flavor
from headgate._core import SendProgress, retry_after_of
from headgate._failures import CallSite, status_error
from headgate._url import escape_segment, redact_secrets, render_path, sanitize_url
from headgate.breaker import CircuitBreaker
from headgate.ratelimit import Rate
from headgate.ratelimit._budget import Pacer
from headgate.ratelimit._budgets import Lane, PathBudgets
from headgate.retry import RetryPolicy
from headgate.retry._classify import Attempted, classify

if TYPE_CHECKING:
    from collections.abc import Callable

    from pytest_codspeed import BenchmarkFixture

_ROWS = 1000
_KEYED_URL = "https://api.example.test/v1/series/observations?series_id=GDP&api_key=0123456789abcdef&limit=100"
_ERROR_TEXT = f"GET {_KEYED_URL}: ConnectError: connection refused while sending token=abc123&page=2"

#: Calls per measurement of a function that takes only microseconds (see the module docstring).
_CALLS = 1000


def _batched[**P](fn: Callable[P, object], *args: P.args, **kwargs: P.kwargs) -> Callable[[], None]:
    """Return a callable that calls ``fn(*args, **kwargs)`` :data:`_CALLS` times, for one measurement."""

    def batch() -> None:
        for _ in range(_CALLS):
            fn(*args, **kwargs)

    return batch


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
_MSGSPEC_ROWS = msgspec_flavor.reader(list[MsgspecRow])
_PYDANTIC_ROWS = pydantic_flavor.reader(list[PydanticRow])


def test_escape_segment_batch(benchmark: BenchmarkFixture) -> None:
    """Measure escaping one path segment that needs percent-encoding."""
    benchmark(_batched(escape_segment, "BTC/USD:spot"))


def test_render_path_batch(benchmark: BenchmarkFixture) -> None:
    """Measure rendering a t-string path with three interpolations and a nested prefix."""
    prefix = t"/api/v3/brokerage"
    account, symbol, side = "a1b2c3", "BTC/USD", "buy"
    benchmark(_batched(lambda: render_path(t"{prefix}/accounts/{account}/orders/{symbol}/{side}")))


def test_sanitize_url_batch(benchmark: BenchmarkFixture) -> None:
    """Measure redacting a credential from a URL with a query string."""
    benchmark(_batched(sanitize_url, _KEYED_URL))


def test_redact_secrets_batch(benchmark: BenchmarkFixture) -> None:
    """Measure redacting credentials from a transport error message."""
    benchmark(_batched(redact_secrets, _ERROR_TEXT))


#: Words of 16 KiB, as a server-controlled value an error message quotes whole: a redirect to an invalid port, a token
#: field that fails to parse. Trying every letter as a start would be quadratic in the word's length. The hyphenated
#: word catches a word boundary that forgets ``-``; the digits exercise the possessive prefix of the pattern.
_LONG_WORDS = {"letters": "a" * 16384, "hyphenated": "a-" * 8192, "digits": "1" * 16384}


@pytest.mark.parametrize("word", list(_LONG_WORDS.values()), ids=list(_LONG_WORDS))
def test_redact_secrets_long_word(benchmark: BenchmarkFixture, word: str) -> None:
    """Measure redacting a transport error that quotes one 16 KiB word, which holds no pair to redact."""
    benchmark(redact_secrets, f"Invalid URL in location header: Invalid port: '{word}'.")


def test_status_error_batch(benchmark: BenchmarkFixture) -> None:
    """Measure building the ``ClientError`` for an undeclared 4xx, including its body snippet."""
    response = httpx2.Response(httpx2.codes.BAD_REQUEST, text="field 'limit' must be <= 1000 " * 40)
    site = CallSite("ExampleClient", "GET", "https://api.example.test/v1/series")
    benchmark(_batched(status_error, response, site))


def test_msgspec_decode_rows(benchmark: BenchmarkFixture) -> None:
    """Measure decoding a 1000-row list body with the msgspec flavor."""
    benchmark(_MSGSPEC_ROWS, _ROWS_RESPONSE)


def test_pydantic_decode_rows(benchmark: BenchmarkFixture) -> None:
    """Measure decoding the same 1000-row list body with the pydantic flavor."""
    benchmark(_PYDANTIC_ROWS, _ROWS_RESPONSE)


def test_breaker_track_batch(benchmark: BenchmarkFixture) -> None:
    """Measure one successful fetch through a closed circuit breaker."""
    breaker = CircuitBreaker(name="api.example.test")

    def fetch() -> None:
        with breaker.track():
            pass

    benchmark(_batched(fetch))


#: A 503 that asks for a wait with a ``Retry-After`` HTTP-date, the costlier of its two forms to parse.
_DATED_503 = httpx2.Response(httpx2.codes.SERVICE_UNAVAILABLE, headers={"Retry-After": "Wed, 21 Oct 2099 07:28:00 GMT"})


def test_retry_backoff_batch(benchmark: BenchmarkFixture) -> None:
    """Measure computing the wait after a 503 that carries a ``Retry-After`` HTTP-date, from reading the header on."""
    policy = RetryPolicy()

    def wait() -> float:
        return policy.wait_after(retry_after_of(_DATED_503), 1)

    benchmark(_batched(wait))


def test_retry_classify_batch(benchmark: BenchmarkFixture) -> None:
    """Measure judging a failed attempt: whether a 503 with a ``Retry-After`` HTTP-date is retryable, and its delay."""
    policy = RetryPolicy()
    attempted = Attempted(method="GET", keyed=False, progress=SendProgress())
    benchmark(_batched(classify, _DATED_503, attempted, policy))


def test_path_budget_lookup_batch(benchmark: BenchmarkFixture) -> None:
    """Measure choosing a budget by URL path among ten routes, where the last route matches."""
    lanes = [Lane(f"/endpoint-{i}", Rate.per_second(10), sets_rate=True) for i in range(10)]
    budgets = PathBudgets(default=Rate.per_second(100), lanes=lanes)
    request = httpx2.Request("GET", "https://api.example.test/v1/endpoint-9/items")
    benchmark(_batched(budgets.for_request, request))


def test_rate_feedback_batch(benchmark: BenchmarkFixture) -> None:
    """Measure feeding a budget a response that carries the IETF rate-limit fields, as every response does."""
    budget = Pacer(Rate.per_second(100))
    response = httpx2.Response(
        httpx2.codes.OK, headers={"RateLimit-Policy": '"default";q=100;w=60', "RateLimit": '"default";r=50;t=30'}
    )
    benchmark(_batched(budget.observe, response, sent_at=budget.now()))
