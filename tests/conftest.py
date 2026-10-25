# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""Autouse fixtures that keep the transport policy (retry, breaker, throttle) real but instant and isolated.

They apply to every test under ``tests/``: a mocked failure is retried as production retries it without sleeping
through the backoff, no breaker state carries from one test to the next, and the rate limiter admits at once. A test
whose subject is the throttle's own timing opts out of the no-op rate-limiter wait with
``@pytest.mark.transport_policy``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from headgate._breaker import HOST_BREAKERS
from headgate._rate_limit import AsyncRateLimiter

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture(autouse=True)
def no_retry_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """Zero the retry backoff, so a mocked failure is retried the way production retries it, without sleeping.

    ``build_retry`` reads the factor when a client is built, so as an autouse fixture this patches before any client
    in the test exists.
    """
    monkeypatch.setattr("headgate._retry._BACKOFF_FACTOR", 0.0)


@pytest.fixture(autouse=True)
def reset_circuit_breakers() -> Iterator[None]:
    """Reset the process-global per-host breakers around every test, so failures never carry into a later test."""
    HOST_BREAKERS.clear()
    yield
    HOST_BREAKERS.clear()


@pytest.fixture(autouse=True)
def no_rate_limit(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the rate limiter's wait a no-op, the one seam patched; which requests acquire a token is unchanged.

    A test whose subject is the throttle's timing carries ``@pytest.mark.transport_policy`` and keeps the real wait.
    """
    if request.node.get_closest_marker("transport_policy"):
        return

    async def admit_at_once(_limiter: AsyncRateLimiter) -> None:
        """Admit the request without waiting for the bucket."""

    monkeypatch.setattr(AsyncRateLimiter, "wait", admit_at_once)
