# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""Retry: the policy a request is re-sent under, and the transport that re-sends it.

:class:`RetryTransport` is the top layer of every client's transport stack (see :mod:`headgate._stack`). It retries
every HTTP method the clients send (``AsyncBaseClient`` installs :data:`ZERO_RETRY` for a ``retry=False`` call),
honours ``Retry-After`` up to a 5 s clamp, re-sends on any ``(httpx2.HTTPError, OSError)``, and re-sends 429 and every
5xx.

The behaviour reproduces ``httpx-retries`` 0.6.0. The golden table ``tests/support/retry_golden.json`` pins every
retry decision, sleep and close across 993 cases, and ``tests/transport/test_retry_golden.py`` replays it; read
``tests/support/retry_matrix.py`` before changing anything here.
"""

from __future__ import annotations

import asyncio
import random
import sys
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING, override

import httpx2

if TYPE_CHECKING:
    from collections.abc import Callable

__all__ = [
    "DEFAULT_BACKOFF_FACTOR",
    "NETWORK_ERRORS",
    "RETRIED_METHODS",
    "ZERO_RETRY",
    "Retry",
    "RetryTransport",
    "build_retry",
]

#: The methods the retry layer re-sends: every method the clients send, POST included (a token fetch, a bulk
#: submission). A non-idempotent write opts out per call with ``retry=False``. Any other method is sent once.
RETRIED_METHODS = frozenset(("GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"))

#: The statuses that are re-sent: 429 and every 5xx.
_RETRIED_STATUSES = frozenset((429, *range(500, 600)))

#: The exceptions that are re-sent, matched with ``isinstance``: every httpx2 error and every ``OSError``. The one
#: definition of a network failure: the cache shares exactly these between waiting requests and passes them through
#: its storage guard, and ``AsyncBaseClient`` maps exactly these onto ``TransportError``.
NETWORK_ERRORS: tuple[type[httpx2.HTTPError], type[OSError]] = (httpx2.HTTPError, OSError)

#: No single wait, backoff or ``Retry-After``, exceeds this many seconds.
_MAX_BACKOFF_WAIT = 5.0

#: The largest exponent the backoff raises 2 to. ``2.0 ** 1024`` overflows a float, so a budget of more than 1023
#: retries would otherwise raise ``OverflowError`` instead of waiting; the clamp leaves every smaller exponent exact.
_MAX_BACKOFF_EXPONENT = 1023

#: The backoff factor production clients use.
DEFAULT_BACKOFF_FACTOR = 0.5

#: The backoff factor :func:`build_retry` reads when a policy is built, so a test suite can zero it before building
#: its clients to make retries instant.
_BACKOFF_FACTOR = DEFAULT_BACKOFF_FACTOR

#: What ``parsedate_to_datetime`` raises for a value it cannot parse. A name rather than a literal tuple in the
#: ``except``, so ``ruff format`` (py314) leaves the clause as written.
_RETRY_AFTER_ERRORS = (TypeError, ValueError)

#: The default jitter source. The operating system's generator, not the ``random`` module's shared one: an
#: application that seeds ``random`` (for reproducible tests, say) would otherwise give every process it runs the same
#: jitter sequence, and the jitter exists to spread their retries apart.
_SYSTEM_RANDOM = random.SystemRandom()


def _retry_after_seconds(value: str) -> float | None:
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


@dataclass(frozen=True, slots=True)
class Retry:
    """One request's retry policy: the budget, the backoff, and how many retries it has spent.

    Immutable (:meth:`increment` returns a copy), so one instance is shared safely between requests. ``total`` counts
    retries, not attempts (see :func:`build_retry`). ``jitter`` returns a uniform draw in ``[0, 1)`` per backoff; it is
    a parameter so a test can replay a seeded sequence.
    """

    total: int
    backoff_factor: float
    attempts_made: int = 0
    jitter: Callable[[], float] = field(default=_SYSTEM_RANDOM.random, repr=False, compare=False)

    @property
    def exhausted(self) -> bool:
        """Whether the budget is spent, so the next failure is returned or raised rather than re-sent."""
        return self.attempts_made >= self.total

    def increment(self) -> Retry:
        """Return a copy that has spent one more retry."""
        return replace(self, attempts_made=self.attempts_made + 1)

    def sleep_seconds(self, failure: httpx2.Response | Exception) -> float:
        """Return how long to wait after *failure* before the next attempt; called on the incremented policy.

        A response's ``Retry-After`` decides when it parses to a positive delay (whole seconds or an HTTP-date),
        capped at :data:`_MAX_BACKOFF_WAIT`. Anything else (a fraction, a negative, ``0``, a past date, garbage, no
        header, an exception) takes the backoff: ``backoff_factor · 2^attempts_made`` times a :attr:`jitter` draw
        (full jitter), capped the same way.
        """
        if isinstance(failure, httpx2.Response):
            header = failure.headers.get("Retry-After", "").strip()
            if header:
                seconds = _retry_after_seconds(header)
                if seconds is not None and (capped := min(seconds, _MAX_BACKOFF_WAIT)) > 0:
                    return capped
        exponent = min(self.attempts_made, _MAX_BACKOFF_EXPONENT)
        # A large factor times 2^1023 overflows to infinity, and infinity times a zero draw is NaN; the largest finite
        # float keeps the product finite while leaving every value that does not overflow exactly as it was.
        backoff = min(self.backoff_factor * (2.0**exponent), sys.float_info.max)
        return min(backoff * self.jitter(), _MAX_BACKOFF_WAIT)


def build_retry(max_attempts: int) -> Retry:
    """Build the retry policy for a client that makes at most ``max_attempts`` sends per request.

    ``total`` counts retries, so it is one less: the default 3 means one send and up to two re-sends, and
    ``max_attempts<=1`` means a single attempt.
    """
    return Retry(total=max(max_attempts - 1, 0), backoff_factor=_BACKOFF_FACTOR)


#: The policy a ``retry=False`` call installs: one send, never re-sent.
ZERO_RETRY = build_retry(0)


class RetryTransport(httpx2.AsyncBaseTransport):
    """Re-send a request on a retried status or exception, waiting between attempts, and close what it wraps.

    * The request's policy is its ``retry`` extension when one is stamped (a ``retry=False`` call stamps
      :data:`ZERO_RETRY`), otherwise :attr:`retry`, which is then stamped on the request.
    * A method outside :data:`RETRIED_METHODS` is sent once, untouched.
    * A retried exception or status is re-sent until the policy is exhausted. A failed response is closed before the
      wait, the policy incremented, and the wait computed on the incremented policy and slept through
      ``asyncio.sleep`` (looked up per call, so a test can patch it).
    * Exhaustion re-raises the last exception or returns the last response. A returned response carries its final
      policy as the ``retry`` extension.

    It emits no log records.
    """

    def __init__(self, *, transport: httpx2.AsyncBaseTransport, retry: Retry) -> None:
        self._transport = transport
        self.retry = retry

    @override
    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        stamped = request.extensions.setdefault("retry", self.retry)
        if not isinstance(stamped, Retry):
            msg = f"the 'retry' request extension must be a Retry, not {type(stamped).__name__}"
            raise TypeError(msg)
        if request.method.upper() not in RETRIED_METHODS:
            return await self._transport.handle_async_request(request)
        policy = stamped
        failure: httpx2.Response | Exception | None = None
        while True:
            if failure is not None:
                if isinstance(failure, httpx2.Response):
                    await failure.aclose()
                policy = policy.increment()
                await asyncio.sleep(policy.sleep_seconds(failure))
            try:
                response = await self._transport.handle_async_request(request)
            except Exception as exc:
                if policy.exhausted or not isinstance(exc, NETWORK_ERRORS):
                    raise
                failure = exc
                continue
            if policy.exhausted or response.status_code not in _RETRIED_STATUSES:
                response.extensions["retry"] = policy
                return response
            failure = response

    @override
    async def aclose(self) -> None:
        """Close the wrapped transport (``httpx2.AsyncBaseTransport.aclose`` is a no-op)."""
        await self._transport.aclose()
