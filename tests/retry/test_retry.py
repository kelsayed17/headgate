# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""``RetryTransport``, ``RetryPolicy`` and ``Backoff``, tested directly.

Covered here: the attempt count, the per-call override on the call's context, the attempts the transport begins, the
backoff's bounds, the injected sleep, and teardown. End-to-end retry behaviour (backoff, ``Retry-After``) is covered
through ``AsyncBaseClient`` in ``tests/client/test_client.py``, and the full decision table in
``test_retry_golden.py``.
"""

from __future__ import annotations

import itertools
from typing import TYPE_CHECKING, override

import httpx2
import pytest

from headgate._core import CallContext, CallInfo
from headgate.retry import NO_RETRY, Backoff, RetryPolicy, RetryTransport
from headgate.retry._policy import RETRIED_METHODS, RETRY_POLICY

if TYPE_CHECKING:
    from collections.abc import Callable

#: An attempt budget larger than the default, for tests that need retries to be available.
_FIVE_ATTEMPTS = 5

#: The default backoff's cap on any wait.
_MAX_WAIT = 5.0

#: The default backoff factor.
_FACTOR = 0.5


def _context_for(request: httpx2.Request) -> CallContext:
    """Stamp a fresh call context on *request* and return it."""
    context = CallContext(CallInfo(method=request.method, url=str(request.url)))
    context.attach(request)
    return context


# --- RetryPolicy and Backoff ---


@pytest.mark.parametrize("attempts", [0, -1])
def test_a_policy_refuses_fewer_than_one_attempt(attempts: int) -> None:
    """Refuse an attempt count below one: one attempt is the fail-fast policy."""
    with pytest.raises(ValueError, match="attempts"):
        RetryPolicy(attempts=attempts)


def test_no_retry_sends_once() -> None:
    """Make ``NO_RETRY`` the one-attempt policy."""
    assert NO_RETRY.attempts == 1


@pytest.mark.parametrize(
    ("build", "name"),
    [(lambda: Backoff(factor=-1.0), "factor"), (lambda: Backoff(max_wait=float("nan")), "max_wait")],
)
def test_a_backoff_refuses_a_negative_or_nan_bound(build: Callable[[], Backoff], name: str) -> None:
    """Refuse a factor or cap that is negative or NaN."""
    with pytest.raises(ValueError, match=name):
        build()


def test_the_retried_methods_include_every_verb_the_client_sends() -> None:
    """Retry every verb ``AsyncBaseClient`` sends, POST included; a call that must not be re-sent opts out."""
    assert {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"} <= RETRIED_METHODS


def test_the_backoff_is_capped_even_for_a_budget_past_the_float_exponent_range() -> None:
    """Wait at most the cap on the 1100th retry, rather than overflowing ``2.0 ** 1100``."""
    retry = 1100
    policy = RetryPolicy(attempts=retry + 1, backoff=Backoff(jitter=lambda: 1.0))

    assert policy.wait_after(httpx2.ConnectError("boom"), retry) == _MAX_WAIT


def test_the_backoff_scales_the_jitter_draw() -> None:
    """Multiply ``factor * 2^retry`` by the backoff's own jitter draw."""
    draw = 0.25
    policy = RetryPolicy(backoff=Backoff(jitter=lambda: draw))

    assert policy.wait_after(httpx2.ConnectError("boom"), 1) == _FACTOR * 2 * draw


def test_the_default_jitter_is_a_uniform_draw() -> None:
    """Draw the default jitter from ``[0, 1)``, so a full backoff never exceeds its nominal size."""
    jitter = Backoff().jitter
    draws = [jitter() for _ in range(100)]

    assert all(0.0 <= draw < 1.0 for draw in draws)
    assert len(set(draws)) > 1, "the draws must vary"


def test_an_overflowing_backoff_with_a_zero_draw_waits_zero_not_nan() -> None:
    """Keep the wait finite when ``factor · 2^retry`` overflows to infinity and the jitter draws zero."""
    retry = 1023
    policy = RetryPolicy(attempts=retry + 1, backoff=Backoff(factor=2.0, jitter=lambda: 0.0))

    assert policy.wait_after(httpx2.Response(httpx2.codes.SERVICE_UNAVAILABLE), retry) == 0.0


# --- RetryTransport ---


async def test_a_calls_own_policy_wins_over_the_transports() -> None:
    """Let the policy in the call context's retry slot win over the transport's default."""
    calls = itertools.count()

    def handler(_req: httpx2.Request) -> httpx2.Response:
        next(calls)
        return httpx2.Response(httpx2.codes.SERVICE_UNAVAILABLE)

    transport = RetryTransport(transport=httpx2.MockTransport(handler), policy=RetryPolicy(attempts=_FIVE_ATTEMPTS))
    request = httpx2.Request("GET", "https://t.test/x")
    _context_for(request).put(RETRY_POLICY, NO_RETRY)
    await transport.handle_async_request(request)

    assert next(calls) == 1, "one attempt: the override is respected"


async def test_each_send_begins_an_attempt_on_the_calls_context() -> None:
    """Begin one attempt per send, so the layers below and the call's error can count them."""
    statuses = iter([httpx2.codes.SERVICE_UNAVAILABLE, httpx2.codes.SERVICE_UNAVAILABLE, httpx2.codes.OK])
    seen: list[int] = []
    request = httpx2.Request("GET", "https://t.test/x")
    context = _context_for(request)

    def handler(_request: httpx2.Request) -> httpx2.Response:
        seen.append(context.attempt)
        return httpx2.Response(next(statuses))

    sleeps: list[float] = []

    async def record(seconds: float, /) -> None:
        sleeps.append(seconds)

    transport = RetryTransport(transport=httpx2.MockTransport(handler), policy=RetryPolicy(), sleep=record)
    response = await transport.handle_async_request(request)

    assert response.status_code == httpx2.codes.OK
    assert seen == [1, 2, 3]
    assert len(sleeps) == len(seen) - 1, "the injected sleep is what waits between attempts"


async def test_a_request_without_a_context_gets_a_standalone_one() -> None:
    """Stamp a standalone context, naming the origin and path only, on a request no client call built."""
    transport = RetryTransport(
        transport=httpx2.MockTransport(lambda _request: httpx2.Response(httpx2.codes.OK)), policy=RetryPolicy()
    )
    request = httpx2.Request("TRACE", "https://user:pw@t.test/x?api_key=secret#frag")
    await transport.handle_async_request(request)

    context = CallContext.find(request)
    assert context is not None
    assert context.info.url == "https://t.test/x"
    assert context.attempt == 1, "a method that is never retried still begins its one attempt"
    assert context.deadline is None


class _RecordsClose(httpx2.AsyncBaseTransport):
    """A transport that records whether it was closed.

    ``AsyncBaseTransport.aclose`` is a no-op, so only an override that reaches this one can set the flag.
    """

    def __init__(self) -> None:
        """Start unclosed."""
        self.closed = False

    @override
    async def aclose(self) -> None:
        self.closed = True


async def test_retry_transport_closes_the_transport_it_wraps() -> None:
    """Delegate teardown, or the client's ``aclose`` stops at the top of the stack.

    httpx2's ``AsyncBaseTransport.aclose()`` is a no-op, so a retry layer that did not override it would strand the
    cache storage and the connection pool below it. The retry matrix never closes the transport, so nothing else pins
    this.
    """
    inner = _RecordsClose()

    await RetryTransport(transport=inner, policy=RetryPolicy()).aclose()

    assert inner.closed is True
