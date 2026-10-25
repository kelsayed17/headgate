# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""A rate (:class:`Rate`), adaptive throttling (:class:`Adaptive`), and the per-path rules (:class:`RateRule`)."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from headgate.ratelimit._budget import RateBudget

__all__ = ["UNLIMITED", "Adaptive", "Rate", "RateRule"]


@dataclass(frozen=True, slots=True)
class Rate:
    """``requests`` per ``period`` seconds, with its unit in the call that builds it.

    ``Rate.per_minute(120)`` and ``Rate.per_second(2)`` are the same rate. Up to ``requests`` (at least one) requests
    may go out back to back; after that they are spaced :attr:`interval` apart.
    """

    requests: float
    period: float

    def __post_init__(self) -> None:
        """Reject a rate that cannot pace: non-positive, NaN, or an infinite period."""
        if math.isnan(self.requests) or self.requests <= 0:
            msg = f"requests must be > 0 (got {self.requests})"
            raise ValueError(msg)
        if not math.isfinite(self.period) or self.period <= 0:
            msg = f"period must be > 0 and finite (got {self.period})"
            raise ValueError(msg)

    @classmethod
    def per_second(cls, requests: float) -> Rate:
        """Return a rate of *requests* per second."""
        return cls(requests=requests, period=1.0)

    @classmethod
    def per_minute(cls, requests: float) -> Rate:
        """Return a rate of *requests* per minute."""
        return cls(requests=requests, period=60.0)

    @property
    def interval(self) -> float:
        """The spacing between two requests once the burst is spent: ``period / requests`` seconds."""
        return self.period / self.requests

    @property
    def burst(self) -> float:
        """How many requests may go out back to back: ``requests``, and at least one."""
        return max(1.0, self.requests)


#: A rate that never throttles in practice (1M req/s): what a budget built with transport policy off paces at.
UNLIMITED = Rate.per_second(1_000_000)


@dataclass(frozen=True, slots=True, kw_only=True)
class Adaptive:
    """Adaptive throttling (AIMD): slow down when the server pushes back, recover while it does not.

    Every 429 or 503, and every response whose rate-limit headers say the quota is spent, multiplies the budget's
    effective rate by ``decrease``, never below ``min_rate`` times the configured rate. A response to a request sent
    before the last decrease does not decrease it again, so one burst of concurrent refusals counts once. Any other
    response below 500 restores ``increase`` times the configured rate for every second since the last change, up to
    the configured rate.
    """

    decrease: float = 0.5
    increase: float = 0.1
    min_rate: float = 0.1

    def __post_init__(self) -> None:
        """Refuse a ``decrease`` outside ``(0, 1)``, a ``min_rate`` outside ``(0, 1]``, and a non-positive increase."""
        if not 0 < self.decrease < 1:
            msg = f"decrease must be > 0 and < 1 (got {self.decrease})"
            raise ValueError(msg)
        if not (self.increase > 0 and math.isfinite(self.increase)):
            msg = f"increase must be > 0 and finite (got {self.increase})"
            raise ValueError(msg)
        if not 0 < self.min_rate <= 1:
            msg = f"min_rate must be > 0 and <= 1 (got {self.min_rate})"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class RateRule:
    """A budget of their own for the requests whose URL path contains ``fragment``.

    ``rate`` is a :class:`Rate`, from which the client builds a budget of its own, or a
    :class:`~headgate.ratelimit.RateBudget` shared with whoever else holds it. The fragment is matched as a substring of
    the URL path, the same idiom :class:`headgate.PathFragmentFilter` uses to scope the cache, and enough for a rule
    that names one endpoint. The first matching rule wins.
    """

    fragment: str
    rate: Rate | RateBudget
