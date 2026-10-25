# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""Retry: the policy a request is re-sent under, and the transport that re-sends it.

:class:`RetryTransport` is the top layer of every client's transport stack (see :mod:`headgate._stack`). It retries
every HTTP method the clients send (``AsyncBaseClient`` installs :data:`ZERO_RETRY` for a ``retry=False`` call),
honours ``Retry-After`` up to a 5 s clamp, re-sends on any ``(httpx2.HTTPError, OSError)``, and re-sends 429 and every
5xx.

The behaviour was specified by ``httpx-retries`` 0.6.0, the library this transport replaced. The golden table
``tests/support/retry_golden.json`` pins every retry decision, sleep and close across 993 cases, and
``tests/test_retry_golden.py`` replays it; see ``tests/support/retry_matrix.py`` before changing anything here.
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import override

import httpx2

__all__ = ["ZERO_RETRY", "Retry", "RetryTransport", "build_retry"]

#: The methods the retry layer re-sends: every method the clients send, POST included (a token fetch, a bulk
#: submission). A non-idempotent write opts out per call with ``retry=False``. Any other method is sent once.
_ALL_METHODS = ("GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS")

#: The statuses that are re-sent: 429 and every 5xx.
_RETRIED_STATUSES = frozenset((429, *range(500, 600)))

#: The exceptions that are re-sent, matched with ``isinstance``: every httpx2 error and every ``OSError``.
_RETRIED_EXCEPTIONS: tuple[type[Exception], ...] = (httpx2.HTTPError, OSError)

#: No single wait, backoff or ``Retry-After``, exceeds this many seconds.
_MAX_BACKOFF_WAIT = 5.0

#: The exponential-backoff factor. Read when a policy is built (:func:`build_retry`), so a test suite can zero it
#: before building its clients to make retries instant.
_BACKOFF_FACTOR = 0.5

#: What ``parsedate_to_datetime`` raises for a value it cannot parse. A name rather than a literal tuple in the
#: ``except``, so ``ruff format`` (py314) leaves the clause as written.
_RETRY_AFTER_ERRORS = (TypeError, ValueError)


def _retry_after_seconds(value: str) -> float | None:
    """Parse a ``Retry-After`` value into seconds; ``None`` when it is neither whole seconds nor an HTTP-date.

    A zone-less date is read as UTC; a past date is 0.
    """
    value = value.strip()
    if value.isascii() and value.isdigit():
        return float(value)
    try:
        when = parsedate_to_datetime(value)
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        return max(0.0, (when - datetime.now(UTC)).total_seconds())
    except _RETRY_AFTER_ERRORS:
        return None


@dataclass(frozen=True, slots=True)
class Retry:
    """One request's retry policy: the budget, the backoff factor, and how many retries it has spent.

    Immutable (:meth:`increment` returns a copy), so one instance is shared safely between requests. ``total`` counts
    RETRIES, not attempts (see :func:`build_retry`).
    """

    total: int
    backoff_factor: float
    attempts_made: int = 0

    @property
    def exhausted(self) -> bool:
        return self.attempts_made >= self.total

    def increment(self) -> Retry:
        return replace(self, attempts_made=self.attempts_made + 1)

    def sleep_seconds(self, failure: httpx2.Response | Exception) -> float:
        """How long to wait after *failure* before the next attempt. Called on the INCREMENTED policy.

        A response's ``Retry-After`` decides when it parses to a positive delay (whole seconds or an HTTP-date),
        capped at :data:`_MAX_BACKOFF_WAIT`. Anything else (a fraction, a negative, ``0``, a past date, garbage, no
        header, an exception) takes the backoff: ``backoff_factor · 2^attempts_made`` times a uniform draw in
        ``[0, 1]`` (full jitter), capped the same way.
        """
        if isinstance(failure, httpx2.Response):
            header = failure.headers.get("Retry-After", "").strip()
            if header:
                seconds = _retry_after_seconds(header)
                if seconds is not None and (capped := min(seconds, _MAX_BACKOFF_WAIT)) > 0:
                    return capped
        if self.attempts_made == 0 or self.backoff_factor == 0:
            return 0.0
        backoff = self.backoff_factor * (2.0**self.attempts_made)
        backoff *= random.uniform(0.0, 1)  # noqa: S311  # jitter, not cryptography
        return min(backoff, _MAX_BACKOFF_WAIT)


def build_retry(max_attempts: int) -> Retry:
    """Build the retry policy for a client that makes at most ``max_attempts`` sends per request.

    ``total`` counts RETRIES, so it is one less: the default 3 means one send and up to two re-sends, and
    ``max_attempts<=1`` means a single attempt.
    """
    return Retry(total=max(max_attempts - 1, 0), backoff_factor=_BACKOFF_FACTOR)


#: The policy a ``retry=False`` call installs: one send, never re-sent.
ZERO_RETRY = build_retry(0)


class RetryTransport(httpx2.AsyncBaseTransport):
    """Re-sends a request on a retried status or exception, waiting between attempts, and closes what it wraps.

    * The request's policy is ``request.extensions.setdefault("retry", self.retry)``, so a :data:`ZERO_RETRY` stamped
      by a ``retry=False`` call wins over the client's default.
    * A method outside :data:`_ALL_METHODS` is sent once, untouched.
    * A retried exception or status is re-sent until the policy is exhausted. A failed response is closed, the policy
      incremented, and the wait computed on the incremented policy and slept through ``asyncio.sleep`` (looked up per
      call, so a test can patch it).
    * Exhaustion re-raises the last exception or returns the last response. A returned response carries its final
      policy as the ``retry`` extension.

    It emits no log records.
    """

    def __init__(self, *, transport: httpx2.AsyncBaseTransport, retry: Retry) -> None:
        self._transport = transport
        self.retry = retry

    @override
    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        # httpx2 types extensions as dict[str, Any]; only this transport and AsyncBaseClient write "retry".
        retry: Retry = request.extensions.setdefault("retry", self.retry)
        if request.method.upper() not in _ALL_METHODS:
            return await self._transport.handle_async_request(request)
        failure: httpx2.Response | Exception | None = None
        while True:
            if failure is not None:
                if isinstance(failure, httpx2.Response):
                    await failure.aclose()
                retry = retry.increment()
                await asyncio.sleep(retry.sleep_seconds(failure))
            try:
                response = await self._transport.handle_async_request(request)
            except Exception as exc:
                if retry.exhausted or not isinstance(exc, _RETRIED_EXCEPTIONS):
                    raise
                failure = exc
                continue
            if retry.exhausted or response.status_code not in _RETRIED_STATUSES:
                response.extensions["retry"] = retry
                return response
            failure = response

    @override
    async def aclose(self) -> None:
        await self._transport.aclose()
