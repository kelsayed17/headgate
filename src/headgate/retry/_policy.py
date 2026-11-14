# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""The retry policy: which failures a call is re-sent after, how often, how long apart, and within what budget.

:class:`RetryPolicy` holds every option the retry layer (:class:`~headgate.retry.RetryTransport`) reads, and the values
it is built from: :class:`Backoff` (the wait between attempts), :class:`RetryShare` (the share of traffic retries may
add, which a live :class:`RetryBudget` spends) and :class:`Hedge` (an opt-in second send for a slow idempotent
request). Each value is frozen and validates on construction, so a bad configuration fails where it is written.

The values the retry layer exchanges with the rest of the stack through a call's
:class:`~headgate._core.CallContext` are declared here as slots: :data:`RETRY_POLICY` (a call's own policy),
:data:`REPLAYABLE_BODY` (``False`` for a body that cannot be sent twice) and :data:`RETRY_OUTCOME` (what the retry
layer concluded about the call's last attempt, which the client folds into the error it raises).
"""

from __future__ import annotations

import math
import sys
import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final, final, override

import httpx2

from headgate._core import Slot, check_kind, current_timing
from headgate.retry._budget import RetryLedger

if TYPE_CHECKING:
    from collections.abc import Callable

    from headgate._core import CallContext, Timing

__all__ = [
    "IDEMPOTENCY_HEADER",
    "IDEMPOTENT_METHODS",
    "NETWORK_ERRORS",
    "NEVER_SENT_ERRORS",
    "REPLAYABLE_BODY",
    "RETRY_OUTCOME",
    "RETRY_POLICY",
    "RETRY_STATUSES",
    "SEND_ONCE",
    "UNRETRYABLE_ERRORS",
    "Backoff",
    "Hedge",
    "RetryBudget",
    "RetryOutcome",
    "RetryPolicy",
    "RetryShare",
    "is_keyed",
]

#: The methods RFC 9110 defines as idempotent that the client sends: re-sent after any retryable failure.
IDEMPOTENT_METHODS: Final = frozenset({"GET", "HEAD", "OPTIONS", "PUT", "DELETE"})

#: The statuses re-sent by default: request timeout, too early (RFC 8470), rate limited, and the transient 5xx.
RETRY_STATUSES: Final = frozenset({408, 425, 429, 500, 502, 503, 504})

#: The exceptions the retry layer considers at all, matched with ``isinstance``: every httpx2 error and every
#: ``OSError``. The one definition of a network failure: the cache shares exactly these between waiting requests, and
#: ``AsyncBaseClient`` maps exactly these onto ``TransportError``.
NETWORK_ERRORS: tuple[type[httpx2.HTTPError], type[OSError]] = (httpx2.HTTPError, OSError)

#: Failures that prove the request never left: no connection was obtained, so no byte of it was written. The fallback
#: rule for a transport that cannot report its send progress.
NEVER_SENT_ERRORS: tuple[type[httpx2.TransportError], ...] = (
    httpx2.ConnectError,
    httpx2.ConnectTimeout,
    httpx2.PoolTimeout,
)

#: Failures that sending the request again cannot cure, so they are never retried: the request's own fault (a header
#: httpx2 refused to encode, a scheme it cannot speak, an invalid URL), and a redirect chain past ``max_redirects``,
#: which the same request follows the same way again.
UNRETRYABLE_ERRORS: tuple[type[Exception], ...] = (
    httpx2.LocalProtocolError,
    httpx2.UnsupportedProtocol,
    httpx2.InvalidURL,
    httpx2.TooManyRedirects,
)

#: The methods a hedge may never be configured for: RFC 9110 does not define them as idempotent.
_UNHEDGEABLE_METHODS: Final = frozenset({"POST", "PATCH"})

#: The largest exponent the backoff raises 2 to. ``2.0 ** 1024`` overflows a float, so a budget of more than 1023
#: retries would otherwise raise ``OverflowError`` instead of waiting; the clamp leaves every smaller exponent exact.
_MAX_BACKOFF_EXPONENT = 1023


def _check_non_negative(name: str, value: float) -> None:
    """Raise ``ValueError`` unless *value* is a finite number >= 0."""
    if not (math.isfinite(value) and value >= 0):
        msg = f"{name} must be >= 0 and finite (got {value})"
        raise ValueError(msg)


def _check_positive(name: str, value: float) -> None:
    """Raise ``ValueError`` unless *value* is a finite number > 0."""
    if not (math.isfinite(value) and value > 0):
        msg = f"{name} must be > 0 and finite (got {value})"
        raise ValueError(msg)


@dataclass(frozen=True, slots=True, kw_only=True)
class Backoff:
    """Exponential backoff with full jitter: the wait before retry *n* is ``factor · 2^n`` times a uniform draw.

    ``max_wait`` caps every backoff wait; a ``Retry-After`` the response sends is waited exactly instead (see
    :attr:`RetryPolicy.max_retry_after`). The draw is the client's (``Timing.random``), so a test pins it there.
    """

    factor: float = 0.5
    max_wait: float = 5.0

    def __post_init__(self) -> None:
        """Refuse a negative, infinite or NaN factor or cap."""
        _check_non_negative("factor", self.factor)
        _check_non_negative("max_wait", self.max_wait)

    def delay(self, retry: int, draw: float) -> float:
        """Return the wait before retry number *retry* (from 1): the exponential times *draw*, capped at ``max_wait``.

        *draw* is a uniform draw in ``[0, 1)``, the jitter.
        """
        exponent = min(retry, _MAX_BACKOFF_EXPONENT)
        # A large factor times 2^1023 overflows to infinity, and infinity times a zero draw is NaN; the largest finite
        # float keeps the product finite while leaving every value that does not overflow exactly as it was.
        nominal = min(self.factor * (2.0**exponent), sys.float_info.max)
        return min(nominal * draw, self.max_wait)


@dataclass(frozen=True, slots=True, kw_only=True)
class RetryShare:
    """A cap on the extra load retries add: retries in the last ``window`` seconds may not exceed a share of traffic.

    A retry (or a hedge) is allowed while the retries over the sliding window number fewer than ``ratio`` times the
    first attempts over it, plus ``min_per_second · window``, a floor so a quiet client can still retry.

    A share is a value, as a :class:`~headgate.Rate` is: a client given one in its policy keeps a budget of its own for
    each distinct share, spent by every call, view and route of that client, so an outage cannot multiply its traffic
    by the attempt count. To spend one budget across clients, build a :class:`RetryBudget` from the share and give
    every client the same object.
    """

    ratio: float = 0.2
    min_per_second: float = 10.0
    window: float = 10.0

    def __post_init__(self) -> None:
        """Refuse a negative ratio or floor, and a window that is not positive."""
        _check_non_negative("ratio", self.ratio)
        _check_non_negative("min_per_second", self.min_per_second)
        _check_positive("window", self.window)


#: The share a policy spends when it names none, and a budget built from none.
_DEFAULT_SHARE: Final = RetryShare()


@final
class RetryBudget:
    """Live shared state: one retry allowance, spent by every client given this same object.

    It is built from a :class:`RetryShare` and spends it over a sliding window, as a :class:`~headgate.RateBudget` is
    built from a :class:`~headgate.Rate`. Two clients whose policies name the same ``RetryBudget`` spend one window
    together: once the first has spent it, the second's retries are refused too. The window runs on *timing*'s
    clock, or, without one, on the timing current when the budget is built. Its ``repr`` shows the share and what the
    window holds now.

    :raises TypeError: for a *share* that is not a :class:`RetryShare`, and, as Python's own, for the old keywords
        (``RetryBudget(ratio=0.1)`` is now ``RetryBudget(RetryShare(ratio=0.1))``).
    """

    __slots__ = ("_ledger", "_lock", "_share")

    def __init__(self, share: RetryShare = _DEFAULT_SHARE, *, timing: Timing | None = None) -> None:
        """Spend *share* over its window, on *timing*'s clock."""
        check_kind("share", share, RetryShare, "a RetryShare, such as RetryShare(ratio=0.1)")
        self._share = share
        self._ledger = RetryLedger(share, (current_timing() if timing is None else timing).clock)
        self._lock = threading.Lock()

    @property
    def share(self) -> RetryShare:
        """The share this budget spends."""
        return self._share

    def record_first(self) -> None:
        """Count a first attempt, which earns retries their share."""
        with self._lock:
            self._ledger.record_first()

    def try_spend(self) -> bool:
        """Count a retry and return ``True`` if the window has room for it, else count nothing and return ``False``."""
        with self._lock:
            return self._ledger.try_spend()

    def refund(self) -> None:
        """Take back a retry :meth:`try_spend` counted but never sent."""
        with self._lock:
            self._ledger.refund()

    @override
    def __repr__(self) -> str:
        with self._lock:
            retries, firsts = self._ledger.counts()
        return f"RetryBudget({self._share!r}, retries={retries}, firsts={firsts})"


@dataclass(frozen=True, slots=True, kw_only=True)
class Hedge:
    """Hedge a slow idempotent request: if no attempt has answered after ``after`` seconds, start another.

    Up to ``max_extra`` hedges run beside the first attempt; the first to answer wins and the rest are cancelled, their
    responses closed. Each hedge counts as an attempt and spends the retry budget.
    """

    after: float = 0.5
    max_extra: int = 1

    def __post_init__(self) -> None:
        """Refuse a delay that is not positive and fewer than one hedge."""
        _check_positive("after", self.after)
        if self.max_extra < 1:
            msg = f"max_extra must be >= 1 (got {self.max_extra})"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True, kw_only=True)
class RetryPolicy:
    """How a call is re-sent: which failures, how many sends, how long apart, and within what budget.

    * ``attempts``: sends per call, the first included (1 never retries); hedges count as sends.
    * ``methods``: the methods treated as idempotent, re-sent after any retryable failure. Any other method (POST,
      PATCH) is re-sent only when the request carries an idempotency key, when nothing of it was written, or when the
      failure proves the server did not process it (425, an HTTP/2 ``REFUSED_STREAM``).
    * ``statuses``: the response statuses that are retryable.
    * ``backoff``: the wait between attempts when the response sets no ``Retry-After``.
    * ``max_retry_after``: the longest ``Retry-After`` (or ``retry-after-ms``) that is waited, exactly. A longer one
      stops the call, and the error it raises carries the delay as ``retry_after``.
    * ``budget``: what retries spend: a :class:`RetryShare`, from which the client builds a budget of its own; a
      :class:`RetryBudget`, shared with every client given the same object; or ``None`` for no cap.
    * ``respect_should_retry``: follow a response's ``x-should-retry: true|false`` over the status rules.
    * ``hedge``: an opt-in :class:`Hedge` for the idempotent ``methods``; refused when ``methods`` include POST or
      PATCH.
    * ``min_attempt_time``: the least time left before the deadline that a retry, a hedge, or the wait before a retry
      is started with. A request's first attempt is sent with whatever time is left.
    """

    attempts: int = 3
    methods: frozenset[str] = IDEMPOTENT_METHODS
    statuses: frozenset[int] = RETRY_STATUSES
    backoff: Backoff = field(default_factory=Backoff)
    max_retry_after: float = 30.0
    budget: RetryShare | RetryBudget | None = field(default_factory=RetryShare)
    respect_should_retry: bool = True
    hedge: Hedge | None = None
    min_attempt_time: float = 0.05

    def __post_init__(self) -> None:
        """Validate every option, upper-case the methods, and refuse a hedge for a non-idempotent method."""
        if self.attempts < 1:
            msg = f"attempts must be >= 1 (got {self.attempts})"
            raise ValueError(msg)
        _check_non_negative("max_retry_after", self.max_retry_after)
        _check_non_negative("min_attempt_time", self.min_attempt_time)
        check_kind(
            "budget", self.budget, (RetryShare, RetryBudget, type(None)), "a RetryShare, a shared RetryBudget, or None"
        )
        methods = frozenset(method.upper() for method in self.methods)
        object.__setattr__(self, "methods", methods)
        object.__setattr__(self, "statuses", frozenset(self.statuses))
        if self.hedge is not None and (unsafe := methods & _UNHEDGEABLE_METHODS):
            msg = f"a hedge may not be configured for non-idempotent methods ({', '.join(sorted(unsafe))})"
            raise ValueError(msg)

    def wait_after(self, retry_after: float | None, retry: int, draw: float) -> float:
        """Return how long to wait before retry number *retry* (from 1), after a failure that asked for *retry_after*.

        *retry_after* is the delay the failed response's ``retry-after-ms`` or ``Retry-After`` asked for, as the
        attempt's classification read it, or ``None``. It is waited exactly and uncapped (the retry layer refuses one
        past :attr:`max_retry_after` instead of waiting it); without one, the wait is :meth:`Backoff.delay` of *draw*.
        """
        return self.backoff.delay(retry, draw) if retry_after is None else retry_after


@dataclass(frozen=True, slots=True)
class RetryOutcome:
    """What the retry layer concluded about a call's last attempt: whether it was retryable, and any delay asked.

    ``failure`` says whether the policy counts the result as a failure at all (a status in ``statuses``, the server's
    ``x-should-retry: true``, or a network failure), as opposed to an answer it passes on. ``retryable`` is the
    policy's verdict: the result is a failure re-sending can cure (not one of :data:`UNRETRYABLE_ERRORS`), and sending
    the request again is safe for its method. *judge*, when the retry layer supplies it, applies the same rules to
    another result of the call (a failure reading the body, which happens after the retry layer has returned): see
    :meth:`about`.
    """

    retryable: bool
    retry_after: float | None = None
    judge: Callable[[httpx2.Response | Exception], RetryOutcome] | None = field(default=None, compare=False, repr=False)
    failure: bool = True

    def about(self, result: httpx2.Response | Exception) -> RetryOutcome:
        """Return the policy's verdict on *result*: judged by :attr:`judge` when there is one, else this verdict."""
        return self if self.judge is None else self.judge(result)


#: The policy that sends once and never re-sends: what ``retry=None`` installs, on a policy, a route, a view or a call.
SEND_ONCE = RetryPolicy(attempts=1)

#: The header an idempotency key is read from when the call names none (a request no client call built).
IDEMPOTENCY_HEADER: Final = "Idempotency-Key"


def is_keyed(context: CallContext, request: httpx2.Request) -> bool:
    """Return whether *request*, of the call *context*, carries an idempotency key, which makes it safe to re-send.

    The key is the call's own, else the ``Idempotency-Key`` header. An empty or blank key identifies nothing.
    """
    key = context.idempotency_key or request.headers.get(IDEMPOTENCY_HEADER)
    return bool(key and key.strip())


#: The call's own retry policy, when it overrides the client's (``retry=None`` or a ``RetryPolicy``).
RETRY_POLICY = Slot[RetryPolicy]("retry")

#: ``False`` when the call's body cannot be sent a second time (a one-shot stream); absent means replayable.
REPLAYABLE_BODY = Slot[bool]("replayable_body")

#: The retry layer's verdict on the call's last attempt, read by the client into the error it raises.
RETRY_OUTCOME = Slot[RetryOutcome]("retry_outcome")
