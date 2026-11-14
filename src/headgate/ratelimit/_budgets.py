# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""A client's budgets, and the choice of budget by URL path.

The budget is chosen by URL path, which every layer sees as the caller built it: the cache layer hands hishel a view
of the request and puts its URL back below hishel, so the path a budget is chosen by is the one the network gets.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from headgate._core import SYSTEM_TIMING, path_segments, policy_is_disabled, segments_hold, url_path
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
    request spends from :attr:`default`. *origin* is the client's own (``base_url``'s): only its responses pause and
    throttle the budgets, so a redirect target, another origin's next page or a token host cannot stop a budget other
    clients share. A request to another origin, an absolute URL the caller named included, is still paced. ``None``
    heeds every origin: what budgets built outside a client get, since every client has an absolute ``base_url``.
    """

    __slots__ = ("_origin", "_rules", "default")

    def __init__(
        self,
        *,
        default: Rate | RateBudget,
        rules: Sequence[RateRule] = (),
        timing: Timing = SYSTEM_TIMING,
        origin: httpx2.Origin | None = None,
    ) -> None:
        """Resolve the default budget and one budget per rule, in rule order, building any from a bare rate."""
        self.default = as_budget(default, timing)
        self._rules = tuple((rule.fragment, as_budget(rule.rate, timing)) for rule in rules)
        self._origin = origin

    def heeds(self, request: httpx2.Request) -> bool:
        """Return whether the response to *request* may pause and throttle its budget: one from the client's origin."""
        return self._origin is None or request.url.origin == self._origin

    def for_request(self, request: httpx2.Request) -> RateBudget:
        """Return the budget *request* spends from: the first matching rule's, else :attr:`default`."""
        if not self._rules:
            return self.default
        found = path_segments(url_path(request.url))
        return next((budget for fragment, budget in self._rules if segments_hold(found, fragment)), self.default)
