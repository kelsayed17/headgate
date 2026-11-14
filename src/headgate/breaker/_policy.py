# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""The breaker option a client is configured with."""

from __future__ import annotations

import math
from dataclasses import dataclass

from headgate._core import check_kind
from headgate.breaker._registry import BreakerRegistry

__all__ = ["BreakerPolicy"]


@dataclass(frozen=True, slots=True, kw_only=True)
class BreakerPolicy:
    """When an origin's breaker trips and when it tests the origin again, and which registry holds the breakers.

    ``failure_threshold`` consecutive failures open the breaker; ``recovery_timeout`` seconds later it admits one probe.
    ``registry`` decides which clients share an origin's breaker. ``None``, the default, gives each client breakers of
    its own; clients whose policies name one :class:`~headgate.breaker.BreakerRegistry` pool their failures in it,
    and must ask for the thresholds it is bound to. ``TransportPolicy(breaker=None)`` turns the breaker off for a
    client.
    """

    failure_threshold: int = 5
    recovery_timeout: float = 60.0
    registry: BreakerRegistry | None = None

    def __post_init__(self) -> None:
        """Refuse a threshold below one, a negative, NaN or infinite cooldown, and a registry of the wrong kind."""
        check_kind("registry", self.registry, (BreakerRegistry, type(None)), "a shared BreakerRegistry, or None")
        if self.failure_threshold < 1:
            msg = f"failure_threshold must be >= 1 (got {self.failure_threshold})"
            raise ValueError(msg)
        if not math.isfinite(self.recovery_timeout) or self.recovery_timeout < 0:
            msg = f"recovery_timeout must be >= 0 and finite (got {self.recovery_timeout})"
            raise ValueError(msg)
