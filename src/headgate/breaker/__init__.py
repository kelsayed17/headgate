# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""The circuit breaker: :class:`BreakerPolicy` configures it, :class:`BreakerRegistry` shares it between clients.

A registry hands out one :class:`CircuitBreaker` per origin, whose :class:`BreakerState` it reports.

Self-contained: it imports only ``httpx2``, the standard library and :mod:`headgate._core`.
"""

from headgate._core import BreakerState
from headgate.breaker._breaker import CircuitBreaker
from headgate.breaker._policy import BreakerPolicy
from headgate.breaker._registry import BreakerRegistry

__all__ = ["BreakerPolicy", "BreakerRegistry", "BreakerState", "CircuitBreaker"]
