# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""The retry policy: how many sends a call gets, and how long to wait between them.

The decision rules (which methods, statuses and exceptions are re-sent, and how a wait is computed) reproduce
``httpx-retries`` 0.6.0. The golden table ``tests/support/retry_golden.json`` pins every decision, sleep and close
across 993 cases, and ``tests/retry/test_retry_golden.py`` replays it; read ``tests/support/retry_matrix.py`` before
changing anything here.

**Extension point.** :class:`RetryPolicy` holds only what these rules read: ``attempts`` and ``backoff``. A rule that
needs more (a method set, a status set, a ``Retry-After`` ceiling, a retry budget, hedging) adds its field here, beside
the rule in :class:`~headgate.retry.RetryTransport` that reads it, so the option and its behaviour arrive together.
"""

from __future__ import annotations

import math
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Final

import httpx2

from headgate._core import RandomSource, Slot, system_random

__all__ = [
    "NETWORK_ERRORS",
    "NO_RETRY",
    "RETRIED_METHODS",
    "RETRIED_STATUSES",
    "RETRY_POLICY",
    "Backoff",
    "RetryPolicy",
    "parse_retry_after",
]

#: The methods the retry layer re-sends: every method the clients send, POST included (a token fetch, a bulk
#: submission). A non-idempotent write opts out per call with ``retry=False``. Any other method is sent once.
RETRIED_METHODS: Final = frozenset(("GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"))

#: The statuses that are re-sent: 429 and every 5xx.
RETRIED_STATUSES: Final = frozenset((429, *range(500, 600)))

#: The exceptions that are re-sent, matched with ``isinstance``: every httpx2 error and every ``OSError``. The one
#: definition of a network failure: the cache shares exactly these between waiting requests and passes them through
#: its storage guard, and ``AsyncBaseClient`` maps exactly these onto ``TransportError``.
NETWORK_ERRORS: tuple[type[httpx2.HTTPError], type[OSError]] = (httpx2.HTTPError, OSError)

#: The largest exponent the backoff raises 2 to. ``2.0 ** 1024`` overflows a float, so a budget of more than 1023
#: retries would otherwise raise ``OverflowError`` instead of waiting; the clamp leaves every smaller exponent exact.
_MAX_BACKOFF_EXPONENT = 1023

#: What ``parsedate_to_datetime`` raises for a value it cannot parse. A name rather than a literal tuple in the
#: ``except``, so ``ruff format`` (py314) leaves the clause as written.
_RETRY_AFTER_ERRORS = (TypeError, ValueError)


def parse_retry_after(value: str) -> float | None:
    """Parse a ``Retry-After`` value into seconds; ``None`` when it is neither whole seconds nor an HTTP-date.

    A zone-less date is read as UTC; a past date is 0.
    """
    value = value.strip()
    if value.isascii() and value.isdigit():
        return float(value)
    try:
        when = parsedate_to_datetime(value)
    except _RETRY_AFTER_ERRORS:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - datetime.now(UTC)).total_seconds())


@dataclass(frozen=True, slots=True, kw_only=True)
class Backoff:
    """Exponential backoff with full jitter: the wait before retry *n* is ``factor · 2^n`` times a ``jitter`` draw.

    ``max_wait`` caps every wait, a ``Retry-After`` included. ``jitter`` returns a uniform draw in ``[0, 1)``; it is a
    parameter so a test can replay a seeded sequence.
    """

    factor: float = 0.5
    max_wait: float = 5.0
    jitter: RandomSource = field(default=system_random, repr=False, compare=False)

    def __post_init__(self) -> None:
        """Refuse a negative or NaN factor or cap."""
        for name, value in (("factor", self.factor), ("max_wait", self.max_wait)):
            if math.isnan(value) or value < 0:
                msg = f"{name} must be >= 0 (got {value})"
                raise ValueError(msg)

    def delay(self, retry: int) -> float:
        """Return the wait before retry number *retry* (from 1): the jittered exponential, capped at ``max_wait``."""
        exponent = min(retry, _MAX_BACKOFF_EXPONENT)
        # A large factor times 2^1023 overflows to infinity, and infinity times a zero draw is NaN; the largest finite
        # float keeps the product finite while leaving every value that does not overflow exactly as it was.
        nominal = min(self.factor * (2.0**exponent), sys.float_info.max)
        return min(nominal * self.jitter(), self.max_wait)


@dataclass(frozen=True, slots=True, kw_only=True)
class RetryPolicy:
    """How a call is re-sent: at most ``attempts`` sends (the first included, so 1 never retries), ``backoff`` apart.

    See the module docstring for the rules it drives and for how it grows.
    """

    attempts: int = 3
    backoff: Backoff = field(default_factory=Backoff)

    def __post_init__(self) -> None:
        """Refuse an attempt count below one."""
        if self.attempts < 1:
            msg = f"attempts must be >= 1 (got {self.attempts})"
            raise ValueError(msg)

    def wait_after(self, failure: httpx2.Response | Exception, retry: int) -> float:
        """Return how long to wait after *failure* before retry number *retry* (from 1).

        A response's ``Retry-After`` decides when it parses to a positive delay (whole seconds or an HTTP-date), capped
        at ``backoff.max_wait``. Anything else (a fraction, a negative, ``0``, a past date, garbage, no header, an
        exception) takes :meth:`Backoff.delay`.
        """
        if isinstance(failure, httpx2.Response):
            header = failure.headers.get("Retry-After", "").strip()
            if header:
                seconds = parse_retry_after(header)
                if seconds is not None and (capped := min(seconds, self.backoff.max_wait)) > 0:
                    return capped
        return self.backoff.delay(retry)


#: The policy that sends once and never re-sends: what ``retry=False`` installs for a call.
NO_RETRY = RetryPolicy(attempts=1)

#: The call's own retry policy, when it overrides the client's (``retry=False`` or a ``RetryPolicy``).
RETRY_POLICY = Slot[RetryPolicy]("retry")
