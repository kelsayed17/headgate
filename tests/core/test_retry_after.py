# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""The one ``Retry-After`` reader the retry layer and the rate budget share."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx2
import pytest

from headgate._core import parse_retry_after, retry_after_of

#: A fixed wall-clock instant for HTTP-date parsing, as a Unix time (2026-10-25T12:00:00Z).
_NOW = datetime(2026, 10, 25, 12, 0, 0, tzinfo=UTC).timestamp()

#: A delay in seconds a header asks for.
_ASKED = 7.0


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("7", 7.0),
        (" 1.5 ", 1.5),
        ("0", 0.0),
        ("", None),
        ("-1", None),
        ("1e3", None),
        ("٣", None),
        ("soon", None),
        ("Sun, 25 Oct 2026 12:00:30 GMT", 30.0),
        ("Sun, 25 Oct 2026 11:00:00 GMT", 0.0),
        ("Sun, 25 Oct 2026 12:00:10 -0000", 10.0),
    ],
)
def test_retry_after_parses_seconds_fractions_and_dates(value: str, expected: float | None) -> None:
    """Read whole or fractional seconds and HTTP-dates (a zone-less one as GMT, a past one as 0); refuse the rest."""
    assert parse_retry_after(value, now=_NOW) == expected


@pytest.mark.parametrize(
    ("status", "headers", "expected"),
    [
        (503, {"Retry-After": "7"}, _ASKED),
        (429, {"retry-after-ms": "1500", "Retry-After": "9"}, 1.5),
        (429, {"retry-after-ms": "soon", "Retry-After": "7"}, _ASKED),
        (429, {}, None),
        (500, {"Retry-After": "7"}, None),
        (408, {"retry-after-ms": "100"}, None),
    ],
)
def test_retry_after_is_read_from_a_429_or_503_only(
    status: int, headers: dict[str, str], expected: float | None
) -> None:
    """Prefer ``retry-after-ms``, fall back to ``Retry-After``, and give the header meaning on 429 and 503 alone."""
    assert retry_after_of(httpx2.Response(status, headers=headers), now=_NOW) == expected


def test_retry_after_measures_a_date_from_the_current_time_by_default() -> None:
    """Measure an HTTP-date from the real wall clock when no time is given."""
    future = datetime.now(UTC) + timedelta(hours=1)
    response = httpx2.Response(503, headers={"Retry-After": future.strftime("%a, %d %b %Y %H:%M:%S GMT")})

    seconds = retry_after_of(response)

    assert seconds is not None
    assert 0 < seconds <= timedelta(hours=1).total_seconds()
