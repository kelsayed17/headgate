# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""Typed events: what a client reports about its calls, in place of log records.

Install an :class:`Observer` (any object with a ``notify(event)`` method) with the client's ``observers=(...)``. Each
call emits :class:`CallStarted`, one :class:`AttemptFinished` per send, and :class:`CallFinished`; the policy layers
add :class:`RetryScheduled`, :class:`RateLimited`, :class:`BreakerChanged`, :class:`CacheChecked` and
:class:`DeprecationNoticed`. Every event is frozen and carries sanitized data only: no header, body or credential. An
observer's exception never reaches the call; it is counted in :func:`headgate.health.observer_failures`.

:class:`CallView` is what :func:`headgate.call_view` returns: the call's :class:`CallInfo`, with its attempt and the
time left, read when asked, for a middleware.
"""

from headgate._core import (
    AttemptFinished,
    BreakerChanged,
    CacheChecked,
    CacheOutcome,
    CallEvent,
    CallFinished,
    CallInfo,
    CallStarted,
    CallView,
    DeprecationNoticed,
    Event,
    Observer,
    RateLimited,
    RetryScheduled,
    rehome,
)

__all__ = [
    "AttemptFinished",
    "BreakerChanged",
    "CacheChecked",
    "CacheOutcome",
    "CallEvent",
    "CallFinished",
    "CallInfo",
    "CallStarted",
    "CallView",
    "DeprecationNoticed",
    "Event",
    "Observer",
    "RateLimited",
    "RetryScheduled",
]

rehome(__name__, globals())
