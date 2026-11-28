# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""Read-only snapshots of the transport tier's degradation state, for an application to export.

This package never logs. Three pieces of state that say something is degraded are readable here: the circuit breakers
of a :class:`~headgate.breaker.BreakerRegistry` you pass (the one your clients share), and two process-wide counts, of
the requests the cache handed to the live transport because it broke and of the observer failures headgate swallowed.
Without these, an open breaker reaches someone only as the ``CircuitOpenError`` its caller saw.

These functions, and the snapshot type the first returns, are the whole read surface. Each returns a fresh copy, so a
caller can neither mutate the state nor see it change mid-iteration; a metrics callback can call them on a timer.
"""

from headgate._core import rehome as _rehome
from headgate._health import BreakerSnapshot, breaker_states, cache_degradations, observer_failures

__all__ = ["BreakerSnapshot", "breaker_states", "cache_degradations", "observer_failures"]

_rehome(__name__, globals())
