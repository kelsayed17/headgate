# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""Rate limiting: the budgets a client spends: :class:`Rate`, :class:`RateBudget` and :class:`Adaptive`.

A path's own budget is set by its :class:`~headgate.Route` (``Route("/exports", rate=Rate.per_minute(6))``).

A client given no rate (``rate=None``) paces nothing of its own, but still waits out the pauses a server asks for. The
layer that spends the budgets is the client's own, in this package's private modules.

Self-contained: it imports only ``httpx2``, the standard library and :mod:`headgate._core`.
"""

from headgate.ratelimit._rate import Adaptive

__all__ = ["Adaptive"]
