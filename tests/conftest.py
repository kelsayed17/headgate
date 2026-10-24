"""Fixtures for the suites whose SUBJECT is the transport policy: retry, breaker, throttle.

These monkeypatch the policy stack so a suite can exercise real retry and breaker behaviour without sleeping through
it. They are autouse for every test under ``tests/``. A test whose subject is the throttle's own timing opts out of the
no-op rate-limiter wait with ``@pytest.mark.transport_policy``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from headgate._async_breaker import _host_breakers
from headgate._rate_limiter import AsyncRateLimiter

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture(autouse=True)
def no_retry_backoff(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Zero the retry backoff, so a mocked connection failure is retried the way production retries it, without sleeping.

    ``_BACKOFF_FACTOR`` is read at client construction (``build_retry`` inside ``AsyncBaseClient.__init__``), so as an
    autouse fixture this patches before any client is built.
    """
    monkeypatch.setattr("headgate._transports._BACKOFF_FACTOR", 0.0)
    yield


@pytest.fixture(autouse=True)
def reset_circuit_breakers() -> Iterator[None]:
    """Reset the process-global per-host breakers around every test, so failures never carry into a later test."""
    _host_breakers.clear()
    yield
    _host_breakers.clear()


@pytest.fixture(autouse=True)
def no_rate_limit(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Zero out the async rate limiter's wait, which is the one seam patched (which requests acquire a token is unchanged).

    A test whose SUBJECT is the throttle's timing carries ``@pytest.mark.transport_policy`` and keeps the real wait.
    """
    if request.node.get_closest_marker("transport_policy"):
        return

    async def _noop(self: AsyncRateLimiter) -> None:
        return None

    monkeypatch.setattr(AsyncRateLimiter, "wait", _noop)
