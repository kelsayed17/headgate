# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""``RetryTransport``, ``Retry`` and ``build_retry``, tested directly.

Covered here: the attempt-count mapping, the per-request override, the extension's type check, the backoff's bounds,
and teardown. End-to-end retry behaviour (backoff, ``Retry-After``) is covered through ``AsyncBaseClient`` in
``tests/client/test_client.py``, and the full decision table in ``test_retry_golden.py``.
"""

from __future__ import annotations

import itertools
from typing import override

import httpx2
import pytest

from headgate._retry import DEFAULT_BACKOFF_FACTOR, RETRIED_METHODS, Retry, RetryTransport, build_retry

#: An attempt budget larger than the one-send default, for tests that need retries to be available.
_FIVE_ATTEMPTS = 5

#: No backoff or ``Retry-After`` wait exceeds this (``headgate._retry``'s clamp).
_MAX_WAIT = 5.0

# --- build_retry: attempt-count mapping ---


@pytest.mark.parametrize(("max_attempts", "retries"), [(3, 2), (1, 0), (0, 0), (-1, 0)])
def test_build_retry_maps_attempts_to_retries(max_attempts: int, retries: int) -> None:
    """Map an attempt count to a retry count: one less, never negative (one attempt is fail-fast)."""
    assert build_retry(max_attempts).total == retries


def test_the_retried_methods_include_every_verb_the_client_sends() -> None:
    """Retry every verb ``AsyncBaseClient`` sends, POST included; a call that must not be re-sent opts out."""
    assert {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"} <= RETRIED_METHODS


# --- Retry: the backoff ---


def test_the_backoff_is_capped_even_for_a_budget_past_the_float_exponent_range() -> None:
    """Wait at most the clamp on the 1100th retry, rather than overflowing ``2.0 ** 1100``."""
    attempts = 1100
    policy = Retry(total=attempts, backoff_factor=DEFAULT_BACKOFF_FACTOR, attempts_made=attempts, jitter=lambda: 1.0)

    assert policy.sleep_seconds(httpx2.ConnectError("boom")) == _MAX_WAIT


def test_the_backoff_scales_the_jitter_draw() -> None:
    """Multiply ``backoff_factor * 2^attempts_made`` by the policy's own jitter draw."""
    draw = 0.25
    policy = Retry(total=2, backoff_factor=DEFAULT_BACKOFF_FACTOR, attempts_made=1, jitter=lambda: draw)

    assert policy.sleep_seconds(httpx2.ConnectError("boom")) == DEFAULT_BACKOFF_FACTOR * 2 * draw


def test_the_default_jitter_is_a_uniform_draw() -> None:
    """Draw the default jitter from ``[0, 1)``, so a full backoff never exceeds its nominal size."""
    draws = [build_retry(3).jitter() for _ in range(100)]

    assert all(0.0 <= draw < 1.0 for draw in draws)
    assert len(set(draws)) > 1, "the draws must vary"


# --- RetryTransport: per-request override ---


async def test_retry_transport_honours_per_request_override() -> None:
    """Let a policy stamped on the request win over the transport's default."""
    calls = itertools.count()

    def handler(_req: httpx2.Request) -> httpx2.Response:
        next(calls)
        return httpx2.Response(httpx2.codes.SERVICE_UNAVAILABLE)

    transport = RetryTransport(transport=httpx2.MockTransport(handler), retry=build_retry(_FIVE_ATTEMPTS))
    async with httpx2.AsyncClient(transport=transport, base_url="https://t.test") as client:
        await client.request("GET", "/x", extensions={"retry": build_retry(1)})

    assert next(calls) == 1, "one attempt: the override is respected"


async def test_the_default_policy_is_stamped_on_the_request() -> None:
    """Stamp the transport's own policy on a request that carries none, so layers below can read it."""
    seen: list[object] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request.extensions.get("retry"))
        return httpx2.Response(httpx2.codes.OK)

    policy = build_retry(_FIVE_ATTEMPTS)
    await RetryTransport(transport=httpx2.MockTransport(handler), retry=policy).handle_async_request(
        httpx2.Request("GET", "https://t.test/x")
    )

    assert seen == [policy]


async def test_a_retry_extension_that_is_not_a_policy_is_refused() -> None:
    """Raise ``TypeError`` for a ``retry`` extension of the wrong type, before anything is sent."""
    sent: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        sent.append(request)
        return httpx2.Response(httpx2.codes.OK)

    transport = RetryTransport(transport=httpx2.MockTransport(handler), retry=build_retry(_FIVE_ATTEMPTS))
    request = httpx2.Request("GET", "https://t.test/x", extensions={"retry": _FIVE_ATTEMPTS})

    with pytest.raises(TypeError, match="must be a Retry, not int"):
        await transport.handle_async_request(request)
    assert sent == []


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

    await RetryTransport(transport=inner, retry=build_retry(3)).aclose()

    assert inner.closed is True
