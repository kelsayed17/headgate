# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""A client's rate budget (:class:`Rate`) and the per-path rules that give some requests another one."""

from __future__ import annotations

import math
from dataclasses import dataclass

__all__ = ["UNLIMITED", "Rate", "RateRule"]


@dataclass(frozen=True, slots=True)
class Rate:
    """``requests`` per ``period`` seconds: a client's rate budget, with its unit in the call that builds it.

    ``Rate.per_minute(120)`` and ``Rate.per_second(2)`` are the same budget.
    """

    requests: float
    period: float

    def __post_init__(self) -> None:
        """Reject a budget that cannot pace: non-positive, NaN, or an infinite period."""
        if math.isnan(self.requests) or self.requests <= 0:
            msg = f"requests must be > 0 (got {self.requests})"
            raise ValueError(msg)
        if not math.isfinite(self.period) or self.period <= 0:
            msg = f"period must be > 0 and finite (got {self.period})"
            raise ValueError(msg)

    @classmethod
    def per_second(cls, requests: float) -> Rate:
        """Return a budget of *requests* per second."""
        return cls(requests=requests, period=1.0)

    @classmethod
    def per_minute(cls, requests: float) -> Rate:
        """Return a budget of *requests* per minute."""
        return cls(requests=requests, period=60.0)


#: A rate that never throttles in practice (1M req/s): what a limiter built with transport policy off paces at.
UNLIMITED = Rate.per_second(1_000_000)


@dataclass(frozen=True, slots=True)
class RateRule:
    """A second rate for the requests whose URL path contains ``fragment``.

    The fragment is matched as a substring of the URL path, the same idiom :class:`headgate.PathFragmentFilter` uses
    to scope the cache, and enough for a rule that names one endpoint. The first matching rule wins.
    """

    fragment: str
    rate: Rate
