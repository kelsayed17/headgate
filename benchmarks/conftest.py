# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""Fixtures that run async benchmarks on one persistent event loop.

pytest-codspeed measures synchronous callables, so each async benchmark runs a batch of operations inside one
coroutine: the event loop's own entry cost is paid once per batch, not once per operation, and what is measured is the
operation.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from headgate.breaker import DEFAULT_REGISTRY

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterator

    from pytest_codspeed import BenchmarkFixture

#: Operations per measured batch for the async benchmarks.
BATCH = 200


@pytest.fixture(scope="session")
def runner() -> Iterator[asyncio.Runner]:
    """Provide one event loop for the whole benchmark session."""
    with asyncio.Runner() as session_runner:
        yield session_runner


@pytest.fixture(autouse=True)
def fresh_breakers() -> Iterator[None]:
    """Start every benchmark with no circuit breaker state carried over."""
    DEFAULT_REGISTRY.clear()
    yield
    DEFAULT_REGISTRY.clear()


type Operation = Callable[[], Awaitable[object]]


@pytest.fixture
def run_batch(benchmark: BenchmarkFixture, runner: asyncio.Runner) -> Callable[[Operation], None]:
    """Return a function that benchmarks ``BATCH`` sequential awaits of an operation."""

    def measure(operation: Operation) -> None:
        async def batch() -> None:
            for _ in range(BATCH):
                await operation()

        runner.run(batch())  # warm up: connection setup, limiter creation, first-call caches
        benchmark(lambda: runner.run(batch()))

    return measure
