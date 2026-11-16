# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""A client's budgets, and the choice of budget by URL path.

The budget is chosen by URL path, which every layer sees as the caller built it: the cache layer hands hishel a view
of the request and puts its URL back below hishel, so the path a budget is chosen by is the one the network gets.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple

from headgate._core import (
    SYSTEM_TIMING,
    CallContext,
    Slot,
    path_segments,
    policy_is_disabled,
    segments_hold,
    url_path,
)
from headgate.ratelimit._budget import RateBudget
from headgate.ratelimit._rate import UNPACED, Rate

if TYPE_CHECKING:
    from collections.abc import Sequence

    import httpx2

    from headgate._core import Timing

__all__ = ["CALL_BUDGET", "Lane", "PathBudgets", "as_budget"]


def as_budget(rate: Rate | RateBudget | None, timing: Timing = SYSTEM_TIMING) -> RateBudget:
    """Return the budget *rate* names: a shared :class:`RateBudget` as it is, or a new one paced on *timing*.

    ``None`` is a budget that paces nothing of its own, but still waits out the server's pauses. Built with transport
    policy off (:func:`headgate.policy.policy_disabled`), it is a private budget that paces nothing, with no
    concurrency cap and no throttling, whatever was asked for, a shared budget included.
    """
    if policy_is_disabled():
        return RateBudget(UNPACED, adaptive=None, timing=timing)
    if isinstance(rate, RateBudget):
        return rate
    return RateBudget(UNPACED if rate is None else rate, timing=timing)


#: The budget a call spends from, put on its context by the client once, from the call's route, so every hop of the
#: call (a redirect's included) spends from it.
CALL_BUDGET = Slot[RateBudget]("rate_budget")


class Lane(NamedTuple):
    """A route as the rate layer sees it: its fragment and, when it sets one, its rate (``None`` paces nothing)."""

    fragment: str
    rate: Rate | RateBudget | None = None
    sets_rate: bool = False


class PathBudgets:
    """A client's budgets, chosen by its routes (see the module docstring for why by URL path, not by extension).

    Every budget is resolved at construction: one per route that sets a rate, built from a bare rate, and
    :attr:`default` for every other request. A call spends from the budget its route names, which the client puts on
    its context; a request no call built (a token mint) is matched by its own path, the first route whose fragment it
    holds deciding, as for a call. *origin* is the client's own (``base_url``'s): only its responses pause and
    throttle the budgets, so a redirect target, another origin's next page or a token host cannot stop a budget other
    clients share. A request to another origin, an absolute URL the caller named included, is still paced. ``None``
    heeds every origin: what budgets built outside a client get, since every client has an absolute ``base_url``.
    """

    __slots__ = ("_lanes", "_origin", "default")

    def __init__(
        self,
        *,
        default: Rate | RateBudget | None,
        lanes: Sequence[Lane] = (),
        timing: Timing = SYSTEM_TIMING,
        origin: httpx2.Origin | None = None,
    ) -> None:
        """Resolve the default budget and one budget per lane that sets a rate, in route order."""
        self.default = as_budget(default, timing)
        self._lanes = tuple(
            (lane.fragment, as_budget(lane.rate, timing) if lane.sets_rate else self.default) for lane in lanes
        )
        self._origin = origin

    def heeds(self, request: httpx2.Request) -> bool:
        """Return whether the response to *request* may pause and throttle its budget: one from the client's origin."""
        return self._origin is None or request.url.origin == self._origin

    def for_path(self, path: str) -> RateBudget:
        """Return the budget a call to the encoded *path* spends from: its first matching route's, else the default."""
        if not self._lanes:
            return self.default
        found = path_segments(path)
        return next((budget for fragment, budget in self._lanes if segments_hold(found, fragment)), self.default)

    def for_request(self, request: httpx2.Request) -> RateBudget:
        """Return the budget *request* spends from: its call's (:data:`CALL_BUDGET`), else the one its path names."""
        context = CallContext.find(request)
        budget = None if context is None else context.get(CALL_BUDGET)
        return self.for_path(url_path(request.url)) if budget is None else budget
