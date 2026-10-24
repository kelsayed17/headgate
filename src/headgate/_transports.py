"""Composable httpx2 transports for the data-client policy stack.

:class:`AsyncBaseClient` builds its ``httpx2.AsyncClient`` over a layered transport stack (assembled by
:func:`headgate._stack.build_http_client`), so the cross-cutting retry, caching and rate-limit policy lives in
reusable transports rather than in hand-rolled control flow inside the client::

    RetryTransport(                  # status/exception retries + backoff + Retry-After
      (ResilientCacheTransport(      # opt-in response cache; a HIT stops here
        RateLimitTransport(          # the rate budget — every network send takes a token
          base_or_curl_transport))))  # real network / curl-cffi impersonation

The per-host CIRCUIT BREAKER is deliberately not in this stack. It is driven from ``AsyncBaseClient._send``, so it can
span the body read as well as the send (see :mod:`headgate._async_breaker`). The breaker sits OUTSIDE the retry
transport, so one request is at most one breaker failure however many times the retry layer re-sent it.

Both policy transports are implemented here, with no third-party retry or limiter dependency:

* **Rate limiting** is :class:`RateLimitTransport`: it wraps an inner transport, which is what lets it sit under the
  cache and over the curl transport.
* **Retry** is :class:`RetryTransport`. It retries every HTTP method (``AsyncBaseClient._request`` installs a zero-retry
  policy for a ``retry=False`` call, one send and no re-send), honours ``Retry-After`` up to a 5 s clamp, retries the
  broad ``(httpx2.HTTPError, OSError)`` tuple, and retries 501. A golden table, ``tests/support/retry_golden.json``,
  pins every retry decision, sleep and close across 993 cases, and ``test_retry_golden.py`` replays it.

The cache transport stays library-backed (hishel's, subclassed as
:class:`headgate._cache.ResilientCacheTransport`).

Rate limiting is the BOTTOM layer, below the opt-in cache: a cache hit spends no token, and each retry spends its own.
The breaker's per-host registry lives in :mod:`headgate._async_breaker`. It is module-level so it can be
inspected and reset in tests, which import it from there.
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING, override

import httpx2

from headgate._rate_limiter import AsyncRateLimiter

if TYPE_CHECKING:
    from collections.abc import Sequence

    from headgate._rate_limiter import Rate

__all__ = [
    "PathLimiters",
    "RateLimitTransport",
    "Retry",
    "RetryTransport",
    "build_retry",
]

#: One non-default pacing rule: ``(name, path fragment, rate)``. The name labels the
#: rule (it was the repository's cache key Previously and :class:`PathLimiters` no
#: longer keys on it); the fragment is matched as a substring of the URL path — the
#: same idiom ``headgate._cache.PathFragmentFilter`` uses to scope the cache, and
#: enough for a rule that names one endpoint. The third element is the rule's :class:`Rate`.
type RateRule = tuple[str, str, Rate]

# Retry policy

#: The methods the retry layer re-sends: every method the clients send. The data layer legitimately retries POSTs
#: (OAuth token fetches, bulk submissions) that the idempotent-only default of httpx-retries (the library this transport replaced)
#: would have skipped; an order placement opts out per call with ``retry=False``. A method outside this
#: set (``TRACE``, ``CONNECT``) is sent once.
_ALL_METHODS = ("GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS")

#: The statuses that are re-sent: 429 and every 5xx, 501 included (reproduced from httpx-retries, not endorsed —
#: see the module docstring).
_RETRIED_STATUSES = frozenset((429, *range(500, 600)))

#: The exceptions that are re-sent, matched with ``isinstance``: every httpx2 error and every ``OSError``. Matching
#: ``httpx2.HTTPError`` by class is why the transport stack must be typed against the same ``httpx2`` hierarchy as the client.
_RETRIED_EXCEPTIONS: tuple[type[Exception], ...] = (httpx2.HTTPError, OSError)

#: No single wait — backoff or ``Retry-After`` — exceeds this many seconds.
_MAX_BACKOFF_WAIT = 5.0

# Exponential-backoff factor (~ the old wait_random_exponential(multiplier=0.5)).
# Module-level so tests can zero it (monkeypatch this attribute) to make
# retry-path tests instant. Read when a policy is BUILT (build_retry), not per sleep.
_BACKOFF_FACTOR = 0.5


#: Parse failures from ``parsedate_to_datetime``: the pair httpx-retries 0.6.0 catches, reproduced. A NAME, not a
#: parenthesized tuple literal: this subtree targets py314 (see ``pyproject.toml``), where ``ruff format`` rewrites
#: ``except (A, B):`` into the unparenthesized PEP 758 form. A name resists it.
_RETRY_AFTER_ERRORS = (TypeError, ValueError)


def _retry_after_seconds(value: str) -> float | None:
    """A ``Retry-After`` value in seconds, or ``None`` when it is neither whole seconds nor an HTTP-date.

    A zone-less date is read as UTC; a past date is 0. Reproduces httpx-retries 0.6.0's ``parse_retry_after``.
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

    Immutable — :meth:`increment` returns a copy — so one instance is shared safely: each client's default and the
    module-level ``_ZERO_RETRY`` a ``retry=False`` call installs. ``total`` counts RETRIES, not attempts (see
    :func:`build_retry`).
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
        capped at :data:`_MAX_BACKOFF_WAIT`. Anything else — a fraction, a negative, ``0``, a past date, garbage, no
        header, an exception — takes the backoff: ``backoff_factor · 2^attempts_made`` times a uniform draw in
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
        backoff *= random.uniform(0.0, 1)
        return min(backoff, _MAX_BACKOFF_WAIT)


def build_retry(max_retries: int) -> Retry:
    """The client's retry policy for ``max_retries`` ATTEMPTS.

    ``max_retries`` is the *attempt* count (the old ``stop_after_attempt(max_retries)`` semantics); ``total`` is
    the number of *retries*, so it is one less. ``max_retries<=1`` means fail-fast (a single attempt, ``total=0``).
    The retried set, the backoff cap and ``Retry-After`` honouring mirror the previous ``_is_transient`` +
    ``wait_random_exponential(multiplier=0.5, max=5)`` configuration.
    """
    return Retry(total=max(max_retries - 1, 0), backoff_factor=_BACKOFF_FACTOR)


class RetryTransport(httpx2.AsyncBaseTransport):
    """Re-sends a request on a retried status or exception, waiting between attempts — and closes what it wraps.

    In-house. It replaces ``httpx_retries.RetryTransport``, used unsubclassed; this class
    reproduces its behaviour as :func:`build_retry` configured it, EXACTLY, pinned case by case by the golden table
    ``tests/support/retry_golden.json``:

    * the request's policy is ``request.extensions.setdefault("retry", self.retry)``, so the ``_ZERO_RETRY`` a
      ``retry=False`` call stamps wins over this client's default;
    * a method outside :data:`_ALL_METHODS` is sent once, untouched;
    * a retried exception or status is re-sent until the policy is exhausted. A failed response is closed, the policy
      incremented, and the wait computed on the incremented policy and slept through ``asyncio.sleep``, looked up per
      call — the seam the test suite patches;
    * exhaustion re-raises the last exception or returns the last response, and a returned response carries its final
      policy as the ``retry`` extension.

    It emits no log records: headgate never logs, so the caller decides what to record. httpx-retries emitted a record
    for every attempt and for an unparseable or zone-less ``Retry-After``. ``_async_transport`` and ``retry`` keep the
    library's names because the tests walk and read them.
    """

    def __init__(self, *, transport: httpx2.AsyncBaseTransport, retry: Retry) -> None:
        self._async_transport = transport
        self.retry = retry

    @override
    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        retry: Retry = request.extensions.setdefault(
            "retry", self.retry
        )  # httpx2 types extensions as dict[str, Any]; only this transport and _request write "retry", always a Retry
        if request.method.upper() not in _ALL_METHODS:
            return await self._async_transport.handle_async_request(request)
        failure: httpx2.Response | Exception | None = None
        while True:
            if failure is not None:
                if isinstance(failure, httpx2.Response):
                    await failure.aclose()
                retry = retry.increment()
                await asyncio.sleep(retry.sleep_seconds(failure))
            try:
                response = await self._async_transport.handle_async_request(request)
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
        await self._async_transport.aclose()


class PathLimiters:
    """Choose a client's limiter by URL path.

    Not by ``request.extensions``: hishel's cache transport rebuilds the request before
    handing it down and forwards only its own ``hishel_*`` keys (measured — httpx2's own
    ``timeout`` does not survive either), so a limiter selected through an extension
    would be invisible to every layer below the cache. The URL survives, which is what
    makes the path the only workable key for a rule declared once per client.

    Every limiter is built before the first request — the rate is the client's
    construction-time decision — so a lookup only routes to one. The first rule whose
    fragment occurs in the URL path wins; anything else paces on :attr:`default`. (This
    replaced httpx-limiter's abstract repository earlier, whose per-identifier memo
    had nothing to memoize once the limiters were prebuilt.)
    """

    __slots__ = ("_rules", "default")

    def __init__(self, *, default: Rate, rules: Sequence[RateRule] = ()) -> None:
        self.default = AsyncRateLimiter(default)
        self._rules = tuple((fragment, AsyncRateLimiter(rate)) for _name, fragment, rate in rules)

    def for_request(self, request: httpx2.Request) -> AsyncRateLimiter:
        path = request.url.path
        return next((limiter for fragment, limiter in self._rules if fragment in path), self.default)


class RateLimitTransport(httpx2.AsyncBaseTransport):
    """Paces every request that reaches the network — and closes what it wraps.

    Placement is the whole point. Below the cache, a HIT short-circuits above this layer
    and spends no token; below retry, every re-send passes through and spends its own.
    Awaiting the limiter in ``_send`` instead (where it lived until this transport
    existed) billed the first attempt only, so a retrying client could exceed its own
    declared rate and a fully-cached workload throttled itself against a network it never
    touched.

    In-house (it replaces httpx-limiter's ``AsyncMultiRateLimitedTransport``,
    whose ``handle_async_request`` was exactly the body below, and whose missing ``aclose``
    override this class had to supply anyway: ``httpx2.AsyncBaseTransport.aclose`` is a
    no-op, so without it the client's teardown would stop here and never reach the cache
    storage or the connection pool below). ``_transport`` is pinned by
    ``test_aclose_reaches_the_transport_below_the_limiter`` and walked by the tests.
    """

    def __init__(self, *, limiters: PathLimiters, transport: httpx2.AsyncBaseTransport) -> None:
        self._limiters = limiters
        self._transport = transport

    @override
    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        async with self._limiters.for_request(request):
            return await self._transport.handle_async_request(request)

    @override
    async def aclose(self) -> None:
        await self._transport.aclose()
