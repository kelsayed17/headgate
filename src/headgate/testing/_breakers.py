# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""Circuit breakers for tests: a fresh registry, so no breaker state crosses from one test to another."""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING

from headgate._breaker_scope import registry_override
from headgate._core import current_timing
from headgate.breaker import BreakerRegistry

if TYPE_CHECKING:
    from collections.abc import Generator

__all__ = ["isolated_breakers"]


@contextmanager
def isolated_breakers() -> Generator[BreakerRegistry]:
    """Give the clients constructed inside the block a fresh :class:`~headgate.breaker.BreakerRegistry`; yield it.

    Every client that would use the process-wide default registry uses this one instead, so failures recorded in the
    block open no breaker outside it, and a breaker another test opened refuses nothing in it. A client configured
    with a registry of its own keeps it. The registry's breakers read the clock of the current timing, so inside
    :meth:`TestClock.install() <headgate.testing.TestClock.install>` their cooldowns run on the test clock.
    Inspect the registry yielded (``registry[host].state``, ``headgate.health.breaker_states(registry)``) to assert on
    them.
    """
    with registry_override(BreakerRegistry(clock=current_timing().clock)) as registry:
        yield registry
