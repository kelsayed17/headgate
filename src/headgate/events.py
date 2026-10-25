# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""Typed events: what a client reports about its calls, in place of log records.

Install an :class:`Observer` (any object with a ``notify(event)`` method) with ``HttpOptions(observers=(...))``. Each
call emits :class:`CallStarted`, one :class:`AttemptFinished` per send, and :class:`CallFinished`; the policy layers
add :class:`RetryScheduled`, :class:`RateLimited`, :class:`BreakerChanged`, :class:`CacheResult` and
:class:`Deprecated`. Every event is frozen and carries sanitized data only: no header, body or credential. An
observer's exception never reaches the call; it is counted in :func:`headgate.health.observer_failures`.
"""

from headgate._core import (
    AttemptFinished,
    BreakerChanged,
    BreakerState,
    CacheOutcome,
    CacheResult,
    CallEvent,
    CallFinished,
    CallInfo,
    CallStarted,
    Deprecated,
    Event,
    Observer,
    RateLimited,
    RetryScheduled,
)

__all__ = [
    "AttemptFinished",
    "BreakerChanged",
    "BreakerState",
    "CacheOutcome",
    "CacheResult",
    "CallEvent",
    "CallFinished",
    "CallInfo",
    "CallStarted",
    "Deprecated",
    "Event",
    "Observer",
    "RateLimited",
    "RetryScheduled",
]
