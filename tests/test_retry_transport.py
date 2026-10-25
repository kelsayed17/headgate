"""Unit tests for the client policy transports (retry).

End-to-end behaviour (retries, Retry-After) is covered through ``AsyncBaseClient`` in
``test_base_client.py``; these lock the transport-level contracts directly: the
attempt-count mapping and the per-request retry override seam.

The circuit breaker used to be a transport and was tested here too. It is driven from
``AsyncBaseClient._send`` now, so that it can span the body read — see
``test_circuit_breaker.py``, which carries those cases against the real seam.
"""

from __future__ import annotations

import itertools
from typing import override

import httpx2

from headgate._rate_limit import PathLimiters, Rate, RateRule
from headgate._retry import _ALL_METHODS, RetryTransport, build_retry

# ---------- build_retry: attempt-count mapping ----------


def test_build_retry_maps_attempts_to_retries() -> None:
    # max_attempts is an *attempt* count; the policy's total is *retries*.
    assert build_retry(3).total == 2
    assert build_retry(1).total == 0  # fail-fast: a single attempt
    assert build_retry(0).total == 0  # clamped, never negative


def test_the_retried_methods_include_every_verb_the_client_sends() -> None:
    # The six verbs AsyncBaseClient sends, POST included: the data layer retries POSTs (OAuth token
    # fetches, bulk submissions); order placement opts out per call with retry=False.
    assert {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"} <= set(_ALL_METHODS)


# ---------- RetryTransport: per-request override ----------


async def test_retry_transport_honours_per_request_override() -> None:
    calls = itertools.count()

    def handler(_req: httpx2.Request) -> httpx2.Response:
        next(calls)
        return httpx2.Response(503)

    base = httpx2.MockTransport(handler)
    transport = RetryTransport(transport=base, retry=build_retry(5))  # default: 5 attempts
    client = httpx2.AsyncClient(transport=transport, base_url="https://t.test")
    # Per-request override to a single attempt (total=0) must win.
    await client.request("GET", "/x", extensions={"retry": build_retry(1)})
    await client.aclose()
    assert next(calls) == 1  # one attempt, override respected


class _RecordsClose(httpx2.AsyncBaseTransport):
    """A transport that records whether it was closed. ``AsyncBaseTransport.aclose`` is a no-op, so only an override
    that reaches this one can set the flag."""

    def __init__(self) -> None:
        self.closed = False

    @override
    async def aclose(self) -> None:
        self.closed = True


async def test_retry_transport_closes_the_transport_it_wraps() -> None:
    """The retry layer must delegate teardown, or the client's ``aclose`` stops at the top of the stack.

    httpx2's ``AsyncBaseTransport.aclose()`` is a no-op, so a retry layer that did not override it would strand the
    cache storage and the connection pool below it. The retry matrix (``support/retry_matrix.py``) never closes the
    transport, so nothing else pins this.
    """
    inner = _RecordsClose()

    await RetryTransport(transport=inner, retry=build_retry(3)).aclose()

    assert inner.closed is True


# ---------- PathLimiters: the limiter a URL path selects ----------


async def test_path_limiters_pick_the_first_matching_rule_and_default_otherwise() -> None:
    # Two rules whose fragments BOTH occur in /api/candles: "first match wins" is only observable when more than one
    # rule can match, and the second is declared after the first so a last-match scan picks the other limiter.
    limiters = PathLimiters(
        default=Rate.per_second(1000),
        rules=[RateRule("/candles", Rate.per_second(1000)), RateRule("/api", Rate.per_second(1000))],
    )
    (_, candles), (_, api) = limiters._rules

    both = httpx2.Request("GET", "https://x/api/candles?g=1")
    assert limiters.for_request(both) is candles, "the first matching rule must win"
    assert limiters.for_request(both) is not api
    assert limiters.for_request(both) is not limiters.default

    only_the_second = httpx2.Request("GET", "https://x/api/ticker")
    assert limiters.for_request(only_the_second) is api, "a request only the later rule matches still reaches it"

    assert limiters.for_request(httpx2.Request("GET", "https://x/ticker")) is limiters.default
