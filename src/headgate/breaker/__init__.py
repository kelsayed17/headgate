# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""The circuit breaker: :class:`~headgate.BreakerPolicy` configures it, :class:`BreakerRegistry` holds it.

A registry holds one breaker per origin, whose :class:`BreakerState` :func:`headgate.health.breaker_snapshots` reports.
Each client keeps a registry of its own; clients given one registry pool their failures in it.

Self-contained: it imports only ``httpx2``, the standard library and headgate's private core.
"""

from headgate._core import BreakerState
from headgate._core import rehome as _rehome
from headgate.breaker._registry import BreakerRegistry

__all__ = ["BreakerRegistry", "BreakerState"]

_rehome(__name__, globals())
