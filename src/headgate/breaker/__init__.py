# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""The circuit breaker: :class:`~headgate.BreakerPolicy` configures it, :class:`BreakerRegistry` holds it.

A registry hands out one :class:`CircuitBreaker` per origin, whose :class:`BreakerState` it reports. Each client keeps a
registry of its own; clients given one registry pool their failures in it.

Self-contained: it imports only ``httpx2``, the standard library and :mod:`headgate._core`.
"""

from headgate._core import BreakerState
from headgate.breaker._breaker import CircuitBreaker
from headgate.breaker._registry import BreakerRegistry

__all__ = ["BreakerRegistry", "BreakerState", "CircuitBreaker"]
