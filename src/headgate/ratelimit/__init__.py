# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""Rate limiting: budgets (:class:`Rate`, :class:`RateBudget`, :class:`RateRule`) and the layer that spends them.

Self-contained: it imports only ``httpx2``, the standard library and :mod:`headgate._core`.
"""

from headgate.ratelimit._budget import BudgetPausedError, RateBudget
from headgate.ratelimit._budgets import PathBudgets
from headgate.ratelimit._rate import UNLIMITED, Adaptive, Rate, RateRule
from headgate.ratelimit._transport import RateLimitTransport

__all__ = [
    "UNLIMITED",
    "Adaptive",
    "BudgetPausedError",
    "PathBudgets",
    "Rate",
    "RateBudget",
    "RateLimitTransport",
    "RateRule",
]
