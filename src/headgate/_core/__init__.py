# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""The shared core every layer may import: time, origins, the per-call context, events, the policy switch, kind checks.

It imports only the standard library and ``httpx2``, so the policy subpackages (:mod:`headgate.retry`,
:mod:`headgate.breaker`, :mod:`headgate.ratelimit`) can depend on it and on nothing else of headgate's. Coordination
between those subpackages goes through what is defined here and is wired together in :mod:`headgate._stack`.
"""

from headgate._core._context import SENDING, CallContext, CallView, SendProgress, Slot, call_info, progress_of
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
from headgate._core._kinds import check_callable, check_collection, check_int, check_kind, check_names, type_text
from headgate._core._origin import origin_of, origin_text
from headgate._core._paths import check_fragment, path_has_fragment, path_segments, segments_hold, url_path
from headgate._core._retry_after import parse_retry_after, retry_after_of
from headgate._core._seams import (
    BREAKER_GATE,
    EXPLICIT_HEADERS,
    BudgetBacklogError,
    BudgetPausedError,
    CredentialCarrier,
    CredentialStamper,
    ExplicitHeaders,
)
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
    "BREAKER_GATE",
    "EXPLICIT_HEADERS",
    "OBSERVER_FAILURES",
    "SENDING",
    "SILENT",
    "SYSTEM_TIMING",
    "AttemptFinished",
    "BreakerChanged",
    "BreakerState",
    "BudgetBacklogError",
    "BudgetPausedError",
    "CacheOutcome",
    "CacheResult",
    "CallContext",
    "CallEvent",
    "CallFinished",
    "CallInfo",
    "CallStarted",
    "CallView",
    "Clock",
    "CredentialCarrier",
    "CredentialStamper",
    "Deprecated",
    "Emitter",
    "Event",
    "ExplicitHeaders",
    "Observer",
    "RandomSource",
    "RateLimited",
    "RetryScheduled",
    "SendProgress",
    "Sleep",
    "Slot",
    "Timing",
    "asyncio_sleep",
    "call_info",
    "check_callable",
    "check_collection",
    "check_fragment",
    "check_int",
    "check_kind",
    "check_names",
    "current_timing",
    "origin_of",
    "origin_text",
    "parse_retry_after",
    "path_has_fragment",
    "path_segments",
    "policy_disabled",
    "policy_is_disabled",
    "progress_of",
    "retry_after_of",
    "segments_hold",
    "system_random",
    "timing_override",
    "type_text",
    "url_path",
]
