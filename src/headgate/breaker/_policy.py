# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""The breaker option a client is configured with."""

import math
from dataclasses import dataclass

__all__ = ["BreakerPolicy"]


@dataclass(frozen=True, slots=True, kw_only=True)
class BreakerPolicy:
    """When an origin's breaker trips and when it tests the origin again.

    ``failure_threshold`` consecutive failures open the breaker; ``recovery_timeout`` seconds later it admits one probe.
    A client given a ``BreakerPolicy`` builds breakers of its own from it, as it builds a rate budget of its own from a
    :class:`~headgate.Rate`; clients that pool their failures share a :class:`~headgate.breaker.BreakerRegistry` built
    from one, given as ``breaker=`` in its place. ``breaker=None`` turns the breaker off for a client.
    """

    failure_threshold: int = 5
    recovery_timeout: float = 60.0

    def __post_init__(self) -> None:
        """Refuse a threshold below one, and a negative, NaN or infinite cooldown."""
        if self.failure_threshold < 1:
            msg = f"failure_threshold must be >= 1 (got {self.failure_threshold})"
            raise ValueError(msg)
        if not math.isfinite(self.recovery_timeout) or self.recovery_timeout < 0:
            msg = f"recovery_timeout must be >= 0 and finite (got {self.recovery_timeout})"
            raise ValueError(msg)
