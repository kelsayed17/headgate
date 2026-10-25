# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""The breaker option a client is configured with."""

from __future__ import annotations

import math
from dataclasses import dataclass

from headgate.breaker._registry import DEFAULT_REGISTRY, BreakerRegistry

__all__ = ["BreakerPolicy"]


@dataclass(frozen=True, slots=True, kw_only=True)
class BreakerPolicy:
    """When a host's breaker trips and when it tests the host again, and which registry holds the breakers.

    ``failure_threshold`` consecutive failures open the breaker; ``recovery_timeout`` seconds later it admits one probe.
    ``registry`` decides which clients share a host's breaker (see :class:`~headgate.breaker.BreakerRegistry`).
    ``TransportPolicy(breaker=None)`` turns the breaker off for a client.
    """

    failure_threshold: int = 5
    recovery_timeout: float = 60.0
    registry: BreakerRegistry = DEFAULT_REGISTRY

    def __post_init__(self) -> None:
        """Refuse a threshold below one, and a negative, NaN or infinite cooldown."""
        if self.failure_threshold < 1:
            msg = f"failure_threshold must be >= 1 (got {self.failure_threshold})"
            raise ValueError(msg)
        if not math.isfinite(self.recovery_timeout) or self.recovery_timeout < 0:
            msg = f"recovery_timeout must be >= 0 and finite (got {self.recovery_timeout})"
            raise ValueError(msg)
