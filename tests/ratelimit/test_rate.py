# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""The values a budget is configured with: ``Rate``'s units and derived spacing, ``Adaptive``'s ranges, ``RateRule``."""

from __future__ import annotations

import pytest

from headgate.ratelimit import Adaptive, Rate, RateBudget, RateRule

#: A rate whose burst is several requests, and one below a request per period.
_BURST = 5
_HALF = 0.5


def test_rate_units_are_in_the_constructor() -> None:
    """Name the unit in the constructor: ``per_second`` is a 1 s period, ``per_minute`` a 60 s one."""
    assert Rate.per_second(2) == Rate(requests=2, period=1.0)
    assert Rate.per_minute(120) == Rate(requests=120, period=60.0)


@pytest.mark.parametrize(
    ("requests", "period"),
    [(0, 1.0), (-1, 1.0), (float("nan"), 1.0), (1, 0.0), (1, -60.0), (1, float("nan")), (1, float("inf"))],
)
def test_a_rate_must_be_positive_and_finite(requests: float, period: float) -> None:
    """Refuse a rate that cannot pace, rather than failing on the first request that has to wait."""
    with pytest.raises(ValueError, match="must be > 0"):
        Rate(requests=requests, period=period)


def test_a_rate_spaces_requests_by_its_interval_after_a_burst_of_at_least_one() -> None:
    """Derive the spacing from the rate, and let a sub-1 rate still send one request at once."""
    assert Rate.per_second(_BURST).interval == 1 / _BURST
    assert Rate.per_second(_BURST).burst == _BURST
    assert Rate(requests=_HALF, period=1.0).interval == 1 / _HALF, "half a request per second is one per 2 s"
    assert Rate(requests=_HALF, period=1.0).burst == 1


def test_adaptive_defaults_halve_the_rate_and_recover_a_tenth_per_second() -> None:
    """Halve on pushback, restore a tenth of the configured rate per second, never below a tenth of it."""
    assert Adaptive() == Adaptive(decrease=0.5, increase=0.1, min_rate=0.1)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("decrease", 0.0),
        ("decrease", 1.0),
        ("decrease", float("nan")),
        ("increase", 0.0),
        ("increase", float("inf")),
        ("increase", float("nan")),
        ("min_rate", 0.0),
        ("min_rate", 1.5),
        ("min_rate", float("nan")),
    ],
)
def test_adaptive_refuses_a_factor_outside_its_range(field: str, value: float) -> None:
    """Refuse a decrease that does not decrease, an increase that does not increase, and a floor outside ``(0, 1]``."""
    with pytest.raises(ValueError, match=field):
        Adaptive(**{field: value})


def test_a_rule_takes_a_rate_or_a_shared_budget() -> None:
    """Accept a bare rate, from which the client builds a budget, or a budget shared with other holders."""
    shared = RateBudget(Rate.per_second(1))
    assert RateRule("/a", shared).rate is shared
    assert RateRule("/b", Rate.per_second(1)).rate == Rate.per_second(1)


def test_a_min_rate_of_the_whole_configured_rate_is_allowed() -> None:
    """Accept ``min_rate=1``, an adaptive throttle that never slows below the configured rate."""
    assert Adaptive(min_rate=1.0).min_rate == 1.0
