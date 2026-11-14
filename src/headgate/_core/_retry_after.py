# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""How long a response asks its client to wait: ``Retry-After`` (RFC 9110 section 10.2.3) and ``retry-after-ms``.

Two layers act on the same answer, so it is read in one place: the retry layer waits it before re-sending, and the rate
budget pauses every sender sharing it until it has passed. Reading it the same way is what makes the two waits one.
"""

from __future__ import annotations

import math
import re
import time
from datetime import UTC
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    import httpx2

__all__ = ["RETRY_AFTER_STATUSES", "parse_retry_after", "retry_after_of"]

#: The statuses a ``Retry-After`` means something on: rate limited (RFC 6585) and unavailable (RFC 9110).
RETRY_AFTER_STATUSES: Final = frozenset({429, 503})

#: A non-negative decimal number of seconds: ``Retry-After`` defines whole seconds, and fractions are accepted too.
_DECIMAL: Final = re.compile(r"\d+(?:\.\d+)?")

#: Milliseconds in a second, for ``retry-after-ms``.
_MS_PER_SECOND: Final = 1000.0

#: What ``parsedate_to_datetime`` raises for a value it cannot parse.
_DATE_ERRORS: Final = (TypeError, ValueError)


def _decimal_seconds(value: str) -> float | None:
    """Return *value* as a finite non-negative number, or ``None`` when it is not a plain ASCII decimal."""
    if not (value.isascii() and _DECIMAL.fullmatch(value)):
        return None
    seconds = float(value)
    return seconds if math.isfinite(seconds) else None


def parse_retry_after(value: str, *, now: float) -> float | None:
    """Return a ``Retry-After`` value as seconds from *now* (a Unix time); ``None`` when it is in neither form.

    Whole and fractional seconds are read as they are. An HTTP-date is measured from *now*: a zone-less one is read as
    GMT, which an HTTP-date always is, and a past one is no wait at all.
    """
    value = value.strip()
    seconds = _decimal_seconds(value)
    if seconds is not None:
        return seconds
    when = _unix_time(value)
    return None if when is None else max(0.0, when - now)


def _unix_time(value: str) -> float | None:
    """Return an HTTP-date as a Unix time, a zone-less one read as GMT; ``None`` when it does not parse."""
    try:
        when = parsedate_to_datetime(value)
    except _DATE_ERRORS:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return when.timestamp()


def retry_after_of(response: httpx2.Response, *, now: float | None = None) -> float | None:
    """Return the seconds a 429 or 503 *response* asks to wait: ``retry-after-ms`` if it parses, else ``Retry-After``.

    ``None`` for any other status, which RFC 9110 and RFC 6585 give the header no meaning on, and when neither header
    is present or parses. An HTTP-date is measured from the response's own ``Date``, which the server's clock wrote as
    it wrote the ``Retry-After``, so a skewed local clock neither drops the wait nor stretches it. Without a ``Date``
    that parses, it is measured from *now*, a Unix time, the system's wall clock by default.
    """
    if response.status_code not in RETRY_AFTER_STATUSES:
        return None
    headers = response.headers
    millis = _decimal_seconds(headers.get("retry-after-ms", "").strip())
    if millis is not None:
        return millis / _MS_PER_SECOND
    header = headers.get("retry-after")
    if header is None:
        return None
    header = header.strip()
    seconds = _decimal_seconds(header)
    if seconds is not None:
        return seconds
    when = _unix_time(header)
    if when is None:
        return None
    # The server's Date is read only for an HTTP-date, and only when sent: a failed parse raises, which costs.
    date = headers.get("date")
    served = None if date is None else _unix_time(date)
    if served is None:
        served = time.time() if now is None else now
    return max(0.0, when - served)
