# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""The suite's fixtures: autouse ones that keep the transport policy real but instant and isolated, and a storage.

They apply to every test under ``tests/``: a mocked failure is retried as production retries it without sleeping
through the backoff, no breaker state carries from one test to the next, and the rate budget admits at once. A test
whose subject is the throttle's own timing opts out of the no-op budget wait with
``@pytest.mark.transport_policy``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import anysqlite
import pytest
from hishel import AsyncSqliteStorage

from headgate.breaker._registry import DEFAULT_REGISTRY
from headgate.ratelimit._budget import Pacer
from headgate.testing import instant_retries

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

    from headgate._core import CallContext


@pytest.fixture(autouse=True)
def instant_retry_sleep() -> Iterator[None]:
    """Build every client with a sleep that returns at once, so a mocked failure is retried without waiting.

    Clients read their timing when they are built, and every client in a test is built inside this fixture's block.
    """
    with instant_retries():
        yield


@pytest.fixture(autouse=True)
def reset_circuit_breakers() -> Iterator[None]:
    """Reset the shared default breaker registry around every test, so failures never carry into a later test."""
    DEFAULT_REGISTRY.clear()
    yield
    DEFAULT_REGISTRY.clear()


@pytest.fixture(autouse=True)
def no_rate_limit(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the rate budget's wait a no-op, the one seam patched; which requests spend from which budget is unchanged.

    A test whose subject is the throttle's timing carries ``@pytest.mark.transport_policy`` and keeps the real wait.
    """
    if request.node.get_closest_marker("transport_policy"):
        return

    async def admit_at_once(_budget: Pacer, _context: CallContext) -> None:
        """Admit the request without waiting for its turn or a server pause."""

    monkeypatch.setattr(Pacer, "wait", admit_at_once)


@pytest.fixture
async def storage() -> AsyncIterator[AsyncSqliteStorage]:
    """Yield a real hishel storage on an in-memory SQLite database, closed after the test."""
    store = AsyncSqliteStorage(connection=await anysqlite.connect(":memory:"))
    try:
        yield store
    finally:
        await store.close()
