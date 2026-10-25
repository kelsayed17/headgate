# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""Typed events: what a client reports about its calls, instead of logging.

Every event is a frozen value carrying sanitized data only: a URL with its credentials redacted, a method, the route
template, statuses, exception type names and durations on the client's clock. No header, body or credential is ever
put in one. :mod:`headgate.events` is the public face of this module.

An :class:`Observer` installed through ``HttpOptions(observers=...)`` receives them. Observers run inline, in the order
they were given, so one should hand slow work to a queue. An exception an observer raises never reaches the call: the
:class:`Emitter` swallows it and counts it in :data:`OBSERVER_FAILURES`, which ``headgate.health.observer_failures``
reads.

The event set is defined here, below the policy subpackages, so that every layer can emit the events it owns through
the call's :class:`~headgate._core.CallContext`.
"""

from __future__ import annotations

import contextlib
import itertools
from collections import Counter
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from datetime import datetime

__all__ = [
    "OBSERVER_FAILURES",
    "SILENT",
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
    "Emitter",
    "Event",
    "Observer",
    "RateLimited",
    "RetryScheduled",
]

#: Process-unique call ids, so an observer can group the events of one call among many concurrent ones.
_CALL_IDS = itertools.count(1)


@dataclass(frozen=True, slots=True, kw_only=True)
class CallInfo:
    """What identifies one logical call in every event about it.

    ``url`` is the URL the call was given, sanitized and bounded; ``route`` is the t-string template the URL was
    rendered from (``"series/{series_id}"``), or ``None`` for a ``str`` URL. ``full_url`` is the absolute URL of the
    request as first built (before any redirect), sanitized: what an observer needs to name the server, as an
    OpenTelemetry span does. It is ``None`` when the client has no observers.
    """

    method: str
    url: str
    route: str | None = None
    full_url: str | None = None
    call_id: int = field(default_factory=lambda: next(_CALL_IDS))


class BreakerState(StrEnum):
    """Circuit-breaker states; the value is what a breaker reports."""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CacheOutcome(StrEnum):
    """What the HTTP cache did for one request."""

    HIT = "hit"
    MISS = "miss"
    REVALIDATED = "revalidated"
    SHARED = "shared"
    DEGRADED = "degraded"


@dataclass(frozen=True, slots=True, kw_only=True)
class CallEvent:
    """Base of every event about one call: :attr:`call` names it."""

    call: CallInfo


@dataclass(frozen=True, slots=True, kw_only=True)
class CallStarted(CallEvent):
    """A call's request was built and is about to be sent."""


@dataclass(frozen=True, slots=True, kw_only=True)
class AttemptFinished(CallEvent):
    """One send of the call finished: a response head arrived (``status_code``) or it raised (``error``, a type name).

    ``attempt`` counts from 1 across the call's sends, retries and followed redirects included; ``elapsed`` is the
    send's duration in seconds on the client's clock.
    """

    attempt: int
    status_code: int | None = None
    error: str | None = None
    elapsed: float


@dataclass(frozen=True, slots=True, kw_only=True)
class RetryScheduled(CallEvent):
    """The retry layer will send the call again after ``delay`` seconds, because of ``reason``."""

    attempt: int
    delay: float
    reason: str


@dataclass(frozen=True, slots=True, kw_only=True)
class RateLimited(CallEvent):
    """The call's rate budget held it back, or its response paused the budget, for ``delay`` seconds.

    ``reason`` says which: ``"paced"`` or ``"throttled"``, the call waited for its turn at the configured rate or at
    the rate adaptive throttling lowered it to; ``"paused"``, it waited out a pause the server asked for; and
    ``"pushback"``, its own response asked for that pause, which every call sharing the budget then waits out.
    """

    delay: float
    reason: str


@dataclass(frozen=True, slots=True, kw_only=True)
class CacheResult(CallEvent):
    """What the HTTP cache did for the call."""

    outcome: CacheOutcome


@dataclass(frozen=True, slots=True, kw_only=True)
class Deprecated(CallEvent):
    """The call's response announced that its endpoint is deprecated (RFC 9745) or will be sunset (RFC 8594).

    * ``deprecated``: the response carried a valid ``Deprecation`` header;
    * ``deprecated_at``: the date it names (in the past: deprecated since then; in the future: deprecated from then),
      or ``None`` for the older ``Deprecation: true`` and for a response with only a ``Sunset`` header;
    * ``sunset_at``: the ``Sunset`` date, after which the endpoint may stop answering, or ``None``;
    * ``link`` / ``sunset_link``: the ``Link`` targets with ``rel="deprecation"`` / ``rel="sunset"`` (where the
      provider documents the change), resolved against the request URL and with credentials redacted.
    """

    deprecated: bool = False
    deprecated_at: datetime | None = None
    sunset_at: datetime | None = None
    link: str | None = None
    sunset_link: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class CallFinished(CallEvent):
    """A call ended: with the final ``status_code``, or raising ``error`` (the ``ClientError`` type name).

    ``attempts`` is how many sends it made; ``elapsed`` its whole duration in seconds on the client's clock.
    """

    status_code: int | None = None
    error: str | None = None
    attempts: int
    elapsed: float


@dataclass(frozen=True, slots=True, kw_only=True)
class BreakerChanged:
    """An origin's circuit breaker moved from ``previous`` to ``state``; ``call`` is the call that moved it, if any."""

    origin: str
    previous: BreakerState
    state: BreakerState
    failures: int
    call: CallInfo | None = None


type Event = (
    CallStarted
    | AttemptFinished
    | RetryScheduled
    | RateLimited
    | BreakerChanged
    | CacheResult
    | Deprecated
    | CallFinished
)
"""Every event a client emits."""


class Observer(Protocol):
    """Receives a client's events. Installed with ``HttpOptions(observers=(...))``."""

    def notify(self, event: Event) -> None:
        """Handle *event*; any exception raised here is counted and swallowed, never raised into the call."""
        ...


#: How many times each observer class raised from ``notify``, since process start.
OBSERVER_FAILURES: Counter[str] = Counter()


class Emitter:
    """Deliver events to a client's observers, isolating the call from any observer's failure."""

    __slots__ = ("_observers",)

    def __init__(self, observers: tuple[Observer, ...] = ()) -> None:
        """Deliver to *observers*, in order."""
        self._observers = observers

    @property
    def active(self) -> bool:
        """Whether anyone is listening, so a hot path can skip building an event nobody receives."""
        return bool(self._observers)

    def emit(self, event: Event) -> None:
        """Hand *event* to every observer; one that raises is counted in :data:`OBSERVER_FAILURES` and skipped."""
        for observer in self._observers:
            delivered = False
            # An observer is the caller's code, outside headgate's error contract: whatever it raises must not
            # change the call's outcome, and headgate never logs, so the count is the whole record.
            with contextlib.suppress(Exception):
                observer.notify(event)
                delivered = True
            if not delivered:
                OBSERVER_FAILURES[type(observer).__qualname__] += 1


#: The emitter of a client with no observers.
SILENT = Emitter()
