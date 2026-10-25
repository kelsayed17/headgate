# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""``RetryPolicy``, ``Backoff``, ``RetryBudget`` and ``Hedge`` as values."""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx2
import pytest

from headgate.retry import IDEMPOTENT_METHODS, NO_RETRY, RETRY_STATUSES, Backoff, Hedge, RetryBudget, RetryPolicy

if TYPE_CHECKING:
    from collections.abc import Callable

#: The default backoff's cap on any backoff wait.
_MAX_WAIT = 5.0

#: The default backoff factor.
_FACTOR = 0.5

# --- Validation ---


@pytest.mark.parametrize(
    ("build", "name"),
    [
        (lambda: RetryPolicy(attempts=0), "attempts"),
        (lambda: RetryPolicy(max_retry_after=-1.0), "max_retry_after"),
        (lambda: RetryPolicy(min_attempt_time=float("nan")), "min_attempt_time"),
        (lambda: Backoff(factor=-1.0), "factor"),
        (lambda: Backoff(max_wait=float("inf")), "max_wait"),
        (lambda: RetryBudget(ratio=-0.1), "ratio"),
        (lambda: RetryBudget(min_per_second=-1.0), "min_per_second"),
        (lambda: RetryBudget(window=0.0), "window"),
        (lambda: Hedge(after=0.0), "after"),
        (lambda: Hedge(max_extra=0), "max_extra"),
    ],
)
def test_an_invalid_value_is_refused_where_it_is_written(build: Callable[[], object], name: str) -> None:
    """Refuse each out-of-range option at construction, naming it."""
    with pytest.raises(ValueError, match=name):
        build()


def test_a_hedge_is_refused_for_a_non_idempotent_method() -> None:
    """Refuse a hedge when the methods treated as idempotent include POST or PATCH."""
    with pytest.raises(ValueError, match="POST"):
        RetryPolicy(methods=frozenset({"GET", "post"}), hedge=Hedge())


def test_the_methods_are_upper_cased_and_the_collections_frozen() -> None:
    """Normalise the method names and freeze both collections, whatever iterable was given."""
    policy = RetryPolicy(methods=frozenset({"get"}), statuses=frozenset({503}))

    assert policy.methods == frozenset({"GET"})
    assert isinstance(policy.statuses, frozenset)


def test_the_defaults_are_the_documented_ones() -> None:
    """Retry the idempotent methods on 408, 425, 429 and the transient 5xx, under a ratio budget."""
    policy = RetryPolicy()

    assert policy.methods == IDEMPOTENT_METHODS == frozenset({"GET", "HEAD", "OPTIONS", "PUT", "DELETE"})
    assert policy.statuses == RETRY_STATUSES == frozenset({408, 425, 429, 500, 502, 503, 504})
    assert policy.budget == RetryBudget(ratio=0.2, min_per_second=10, window=10.0)
    assert policy.hedge is None
    assert NO_RETRY.attempts == 1


# --- Backoff ---


def test_the_backoff_scales_the_jitter_draw() -> None:
    """Multiply ``factor * 2^retry`` by the backoff's own jitter draw."""
    draw = 0.25
    assert Backoff(jitter=lambda: draw).delay(1) == _FACTOR * 2 * draw


def test_the_backoff_is_capped_even_past_the_float_exponent_range() -> None:
    """Wait at most the cap on the 1100th retry, rather than overflowing ``2.0 ** 1100``."""
    assert Backoff(jitter=lambda: 1.0).delay(1100) == _MAX_WAIT


def test_an_overflowing_backoff_with_a_zero_draw_waits_zero_not_nan() -> None:
    """Keep the wait finite when ``factor · 2^retry`` overflows to infinity and the jitter draws zero."""
    assert Backoff(factor=2.0, jitter=lambda: 0.0).delay(1023) == 0.0


def test_the_default_jitter_is_a_uniform_draw() -> None:
    """Draw the default jitter from ``[0, 1)``, so a full backoff never exceeds its nominal size."""
    jitter = Backoff().jitter
    draws = [jitter() for _ in range(100)]

    assert all(0.0 <= draw < 1.0 for draw in draws)
    assert len(set(draws)) > 1, "the draws must vary"


# --- Waits ---


def test_wait_after_waits_a_retry_after_exactly_and_backs_off_otherwise() -> None:
    """Wait the header's delay uncapped when it parses, and the backoff after anything else."""
    policy = RetryPolicy(backoff=Backoff(jitter=lambda: 1.0))
    long_wait = 60.0
    asked = httpx2.Response(503, headers={"Retry-After": f"{long_wait:g}"})

    assert policy.wait_after(asked, 1) == long_wait, "past the backoff's cap: max_retry_after governs instead"
    assert policy.wait_after(httpx2.Response(503), 1) == _FACTOR * 2
    assert policy.wait_after(httpx2.ConnectError("boom"), 1) == _FACTOR * 2
