# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""The circuit breaker: :class:`BreakerPolicy` configures it, :class:`BreakerRegistry` shares it between clients.

Self-contained: it imports only ``httpx2``, the standard library and :mod:`headgate._core`.
"""

from headgate._core import BreakerState
from headgate.breaker._breaker import BreakerOpenError, CircuitBreaker, Transition, Verdict
from headgate.breaker._policy import BreakerPolicy
from headgate.breaker._registry import DEFAULT_MAX_HOSTS, DEFAULT_REGISTRY, BreakerRegistry

__all__ = [
    "DEFAULT_MAX_HOSTS",
    "DEFAULT_REGISTRY",
    "BreakerOpenError",
    "BreakerPolicy",
    "BreakerRegistry",
    "BreakerState",
    "CircuitBreaker",
    "Transition",
    "Verdict",
]
