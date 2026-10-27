# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""Rate limiting: the budgets a client spends: :class:`Rate`, :class:`RateBudget`, :class:`RateRule`, :class:`Adaptive`.

:data:`UNLIMITED` is a rate no client reaches. The layer that spends the budgets is the client's own, in this package's
private modules.

Self-contained: it imports only ``httpx2``, the standard library and :mod:`headgate._core`.
"""

from headgate.ratelimit._budget import RateBudget
from headgate.ratelimit._rate import UNLIMITED, Adaptive, Rate, RateRule

__all__ = ["UNLIMITED", "Adaptive", "Rate", "RateBudget", "RateRule"]
