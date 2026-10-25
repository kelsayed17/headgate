# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""Autouse fixtures that keep the transport policy (retry, breaker, throttle) real but instant and isolated.

They apply to every test under ``tests/``: a mocked failure is retried as production retries it without sleeping
through the backoff, no breaker state carries from one test to the next, and the rate limiter admits at once. A test
whose subject is the throttle's own timing opts out of the no-op rate-limiter wait with
``@pytest.mark.transport_policy``.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from headgate._core import Timing, timing_override
from headgate.breaker import DEFAULT_REGISTRY
from headgate.ratelimit import AsyncRateLimiter

if TYPE_CHECKING:
    from collections.abc import Iterator


async def _yield_only(_seconds: float, /) -> None:
    """Yield to the event loop once instead of sleeping, as a zero-second backoff would."""
    await asyncio.sleep(0)


@pytest.fixture(autouse=True)
def instant_retry_sleep() -> Iterator[None]:
    """Build every client with a sleep that returns at once, so a mocked failure is retried without waiting.

    Clients read their timing when they are built, and every client in a test is built inside this fixture's block.
    """
    with timing_override(Timing(sleep=_yield_only)):
        yield


@pytest.fixture(autouse=True)
def reset_circuit_breakers() -> Iterator[None]:
    """Reset the shared default breaker registry around every test, so failures never carry into a later test."""
    DEFAULT_REGISTRY.clear()
    yield
    DEFAULT_REGISTRY.clear()


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
