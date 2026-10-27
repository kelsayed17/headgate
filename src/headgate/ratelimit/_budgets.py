# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""A client's budgets, and the choice of budget by URL path.

The budget is chosen by URL path rather than by a request extension: hishel's cache transport rebuilds the request
before handing it down and forwards only its own ``hishel_*`` keys, so a budget chosen through an extension would be
invisible below the cache. The URL survives.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from headgate._core import SYSTEM_TIMING, path_has_fragment, policy_is_disabled
from headgate.ratelimit._budget import RateBudget
from headgate.ratelimit._rate import UNLIMITED, Rate

if TYPE_CHECKING:
    from collections.abc import Sequence

    import httpx2

    from headgate._core import Timing
    from headgate.ratelimit._rate import RateRule

__all__ = ["PathBudgets", "as_budget"]


def as_budget(rate: Rate | RateBudget, timing: Timing = SYSTEM_TIMING) -> RateBudget:
    """Return the budget *rate* names: a shared :class:`RateBudget` as it is, or a new one paced on *timing*.

    Built with transport policy off (:func:`headgate.policy.policy_disabled`), it is a private budget at
    :data:`UNLIMITED` with no concurrency cap and no throttling, whatever was asked for, a shared budget included.
    """
    if policy_is_disabled():
        return RateBudget(UNLIMITED, adaptive=None, timing=timing)
    if isinstance(rate, RateBudget):
        return rate
    return RateBudget(rate, timing=timing)


class PathBudgets:
    """A client's budgets, chosen by URL path (see the module docstring for why not by extension).

    Every budget is resolved at construction. The first rule whose fragment the URL path holds wins; any other
    request spends from :attr:`default`.
    """

    __slots__ = ("_rules", "default")

    def __init__(
        self, *, default: Rate | RateBudget, rules: Sequence[RateRule] = (), timing: Timing = SYSTEM_TIMING
    ) -> None:
        """Resolve the default budget and one budget per rule, in rule order, building any from a bare rate."""
        self.default = as_budget(default, timing)
        self._rules = tuple((rule.fragment, as_budget(rule.rate, timing)) for rule in rules)

    def for_request(self, request: httpx2.Request) -> RateBudget:
        """Return the budget *request* spends from: the first matching rule's, else :attr:`default`."""
        path = request.url.path
        return next((budget for fragment, budget in self._rules if path_has_fragment(path, fragment)), self.default)
