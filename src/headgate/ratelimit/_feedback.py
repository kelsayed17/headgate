# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""What a response says about the rate budget: whether the server pushed back, and how long to pause.

Three sources are read, in this order of precedence:

* ``retry-after-ms`` or ``Retry-After`` on a 429 or 503, read as the retry layer reads them
  (:func:`headgate._core.retry_after_of`), so the pause the budget takes is the wait the retry layer makes;
* ``RateLimit`` with ``RateLimit-Policy`` (draft-ietf-httpapi-ratelimit-headers-11): Structured Field Lists of policy
  items. An item with ``r=0`` (no quota left) is exhausted, and its ``t`` is the seconds until quota returns. An item
  naming a policy whose ``qu`` is not ``"requests"`` (``content-bytes``, ``concurrent-requests``) is not about request
  rate and is skipped. When several items are exhausted, the longest wait wins. Per the draft, a malformed field is
  ignored and ``Retry-After`` takes precedence over ``t``;
* ``X-RateLimit-Remaining`` with ``X-RateLimit-Reset``, the widespread de facto pair, read when ``RateLimit`` is absent.
  Its reset is seconds to wait or an absolute Unix time, told apart by size: a value of at least
  :data:`_EPOCH_SECONDS_FLOOR` (September 2001 as a Unix time, and 31 years as a delay) is a time, and one of at
  least :data:`_EPOCH_MILLIS_FLOOR` is a time in milliseconds.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

import httpx2

from headgate._core import retry_after_of
from headgate.ratelimit._fields import parse_list

if TYPE_CHECKING:
    from headgate.ratelimit._fields import Item

__all__ = ["Feedback", "read_feedback"]

#: The statuses that say the server refused for load or quota.
_PUSHBACK_STATUSES: Final = frozenset({httpx2.codes.TOO_MANY_REQUESTS, httpx2.codes.SERVICE_UNAVAILABLE})

#: The quota unit a policy limits requests in; the draft's default when ``qu`` is absent.
_REQUESTS_UNIT: Final = "requests"

#: Above these, an ``X-RateLimit-Reset`` is an absolute Unix time in seconds, or in milliseconds.
_EPOCH_SECONDS_FLOOR: Final = 1_000_000_000
_EPOCH_MILLIS_FLOOR: Final = 1_000_000_000_000
_MILLIS: Final = 1000


@dataclass(frozen=True, slots=True)
class Feedback:
    """What one response said: ``pushback`` (refused, or quota spent) and ``pause``, seconds before the next send."""

    pushback: bool = False
    pause: float | None = None


#: A response that said nothing about the budget.
_NOTHING: Final = Feedback()


def read_feedback(response: httpx2.Response, *, now: float) -> Feedback:
    """Return what *response* says about the rate budget; *now* is the wall-clock Unix time, for absolute times."""
    headers = response.headers
    refused = response.status_code in _PUSHBACK_STATUSES
    retry_after = retry_after_of(response, now=now)
    exhausted, reset = _ietf_quota(headers)
    if exhausted is None:
        exhausted, reset = _de_facto_quota(headers, now=now)
    if not (refused or exhausted):
        return _NOTHING
    pause = retry_after if retry_after is not None else reset if exhausted else None
    return Feedback(pushback=True, pause=pause)


def _ietf_quota(headers: httpx2.Headers) -> tuple[bool | None, float | None]:
    """Read ``RateLimit``: whether a request policy is exhausted and the longest ``t``; ``None`` when unreadable."""
    items = parse_list(headers.get("RateLimit", "")) or []
    if not items:
        return None, None
    units = _policy_units(headers)
    exhausted = False
    reset: float | None = None
    for item in items:
        if _count(item.params.get("r")) != 0 or units.get(str(item.value), _REQUESTS_UNIT) != _REQUESTS_UNIT:
            continue
        exhausted = True
        window = _count(item.params.get("t"))
        if window is not None:
            reset = float(window) if reset is None else max(reset, float(window))
    return exhausted, reset


def _policy_units(headers: httpx2.Headers) -> dict[str, str]:
    """Return each ``RateLimit-Policy`` policy's quota unit, by name; empty when the field is absent or malformed."""
    policies: list[Item] = parse_list(headers.get("RateLimit-Policy", "")) or []
    return {str(policy.value): str(policy.params.get("qu", _REQUESTS_UNIT)) for policy in policies}


def _count(value: object) -> int | None:
    """Return *value* when it is a non-negative Structured Field Integer (a Boolean is not one), else ``None``."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _de_facto_quota(headers: httpx2.Headers, *, now: float) -> tuple[bool, float | None]:
    """Read ``X-RateLimit-Remaining`` / ``-Reset``: whether the quota is spent, and the seconds to its reset."""
    remaining = _numbers(headers.get("X-RateLimit-Remaining", ""))
    if not remaining or min(remaining) > 0:
        return False, None
    resets = _numbers(headers.get("X-RateLimit-Reset", ""))
    if not resets:
        return True, None
    reset = max(resets)
    if reset >= _EPOCH_MILLIS_FLOOR:
        reset = reset / _MILLIS - now
    elif reset >= _EPOCH_SECONDS_FLOOR:
        reset -= now
    return True, max(0.0, reset)


def _numbers(value: str) -> list[float]:
    """Return the non-negative numbers in a comma-separated header value; any part that is not one is skipped."""
    numbers: list[float] = []
    for part in value.split(","):
        try:
            number = float(part)
        except ValueError:
            continue
        if number >= 0 and math.isfinite(number):
            numbers.append(number)
    return numbers
