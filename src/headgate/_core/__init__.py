# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""The shared core every layer may import: time, origins, the per-call context, events and the policy switch.

It imports only the standard library and ``httpx2``, so the policy subpackages (:mod:`headgate.retry`,
:mod:`headgate.breaker`, :mod:`headgate.ratelimit`) can depend on it and on nothing else of headgate's. Coordination
between those subpackages goes through what is defined here and is wired together in :mod:`headgate._stack`.
"""

from headgate._core._context import CallContext, SendProgress, Slot
from headgate._core._events import (
    OBSERVER_FAILURES,
    SILENT,
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
    Emitter,
    Event,
    Observer,
    RateLimited,
    RetryScheduled,
)
from headgate._core._origin import origin_of, origin_text, same_origin
from headgate._core._switch import policy_disabled, policy_is_disabled
from headgate._core._time import (
    SYSTEM_TIMING,
    Clock,
    RandomSource,
    Sleep,
    Timing,
    asyncio_sleep,
    current_timing,
    system_random,
    timing_override,
)

__all__ = [
    "OBSERVER_FAILURES",
    "SILENT",
    "SYSTEM_TIMING",
    "AttemptFinished",
    "BreakerChanged",
    "BreakerState",
    "CacheOutcome",
    "CacheResult",
    "CallContext",
    "CallEvent",
    "CallFinished",
    "CallInfo",
    "CallStarted",
    "Clock",
    "Deprecated",
    "Emitter",
    "Event",
    "Observer",
    "RandomSource",
    "RateLimited",
    "RetryScheduled",
    "SendProgress",
    "Sleep",
    "Slot",
    "Timing",
    "asyncio_sleep",
    "current_timing",
    "origin_of",
    "origin_text",
    "policy_disabled",
    "policy_is_disabled",
    "same_origin",
    "system_random",
    "timing_override",
]
