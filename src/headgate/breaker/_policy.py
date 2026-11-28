# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""The breaker option a client is configured with."""

import math
from dataclasses import dataclass
from typing import final

from headgate._core import check_int, check_number, sealed

__all__ = ["BreakerPolicy"]


@final
@sealed("set failure_threshold and recovery_timeout, which are all the breaker reads")
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
        """Refuse a value of the wrong kind, a threshold below one, and a negative, NaN or infinite cooldown."""
        check_int("failure_threshold", self.failure_threshold, "a number of failures, an int of at least 1")
        check_number("recovery_timeout", self.recovery_timeout, "seconds, a finite number of 0 or more")
        if self.failure_threshold < 1:
            msg = f"failure_threshold must be >= 1 (got {self.failure_threshold})"
            raise ValueError(msg)
        if not math.isfinite(self.recovery_timeout) or self.recovery_timeout < 0:
            msg = f"recovery_timeout must be >= 0 and finite (got {self.recovery_timeout})"
            raise ValueError(msg)
