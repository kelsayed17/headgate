# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""Deprecation (RFC 9745) and Sunset (RFC 8594) headers become one ``Deprecated`` event per call."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx2
import pytest

from headgate import as_bytes, nothing
from headgate._deprecation import parse_deprecation, parse_http_date
from headgate.events import Deprecated
from headgate.testing import FakeTransport
from tests.observe._support import Recorder, client

#: 2023-06-30T23:59:59Z, RFC 9745's example, as seconds since the epoch and as an HTTP-date.
_EPOCH = 1688169599
_WHEN = datetime(2023, 6, 30, 23, 59, 59, tzinfo=UTC)
_HTTP_DATE = "Fri, 30 Jun 2023 23:59:59 GMT"
_SUNSET = "Sun, 30 Jun 2024 23:59:59 GMT"
_SUNSET_WHEN = datetime(2024, 6, 30, 23, 59, 59, tzinfo=UTC)
_TWO_HOURS = timedelta(hours=2)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        pytest.param(f"@{_EPOCH}", (True, _WHEN), id="rfc-9745-date"),
        pytest.param(f" @{_EPOCH} ", (True, _WHEN), id="padded"),
        pytest.param("true", (True, None), id="draft-true"),
        pytest.param("TRUE", (True, None), id="draft-true-any-case"),
        pytest.param(_HTTP_DATE, (True, _WHEN), id="draft-http-date"),
        pytest.param(
            "Fri, 30 Jun 2023 23:59:59 +0200", (True, datetime(2023, 6, 30, 21, 59, 59, tzinfo=UTC)), id="zone"
        ),
        pytest.param("@" + "9" * 15, (False, None), id="past-datetime-range"),
        pytest.param("@1.5", (False, None), id="decimal-not-date"),
        pytest.param("soon", (False, None), id="garbage"),
        pytest.param("", (False, None), id="empty"),
    ],
)
def test_both_deprecation_formats_parse(value: str, expected: tuple[bool, datetime | None]) -> None:
    """Read RFC 9745's ``@<epoch>`` and the drafts' ``true`` and HTTP-date; refuse anything else."""
    assert parse_deprecation(value) == expected


def test_a_zoneless_http_date_is_utc() -> None:
    """Read a date without a zone (``-0000``) as UTC."""
    assert parse_http_date("Fri, 30 Jun 2023 23:59:59 -0000") == _WHEN


def _headers_call(headers: dict[str, str]) -> tuple[FakeTransport, Recorder]:
    """Return a fake answering ``GET /v1/old`` with *headers*, and a recorder."""
    return FakeTransport().route("GET", "/v1/old", httpx2.Response(200, headers=headers)), Recorder()


async def test_a_deprecated_endpoint_reports_its_date_sunset_and_links() -> None:
    """Emit one event with both dates and both links, resolved against the request and sanitized."""
    fake, recorder = _headers_call(
        {
            "Deprecation": f"@{_EPOCH}",
            "Sunset": _SUNSET,
            "Link": '<https://docs.example.test/migrate?sig=abc>; rel="deprecation", </v2/policy>; rel="sunset"',
        }
    )
    await client(fake, recorder).get("old", expect=nothing)

    (event,) = recorder.of(Deprecated)
    assert event == Deprecated(
        call=event.call,
        deprecated=True,
        deprecated_at=_WHEN,
        sunset_at=_SUNSET_WHEN,
        link="https://docs.example.test/migrate?sig=%2A%2A%2A",
        sunset_link="https://api.example.test/v2/policy",
    )
    assert event.call.url == "old"


async def test_a_sunset_alone_is_reported_without_deprecation() -> None:
    """Report a ``Sunset`` header on its own, not deprecated, with no links."""
    fake, recorder = _headers_call({"Sunset": _SUNSET})
    await client(fake, recorder).get("old", expect=nothing)

    (event,) = recorder.of(Deprecated)
    assert (event.deprecated, event.deprecated_at, event.sunset_at, event.link) == (False, None, _SUNSET_WHEN, None)


@pytest.mark.parametrize(
    "headers",
    [
        pytest.param({}, id="neither"),
        pytest.param({"Deprecation": "maybe"}, id="invalid-deprecation"),
        pytest.param({"Sunset": "later"}, id="invalid-sunset"),
        pytest.param({"Link": '</d>; rel="deprecation"'}, id="link-alone"),
    ],
)
async def test_nothing_valid_announces_nothing(headers: dict[str, str]) -> None:
    """Emit no event without a valid ``Deprecation`` or ``Sunset`` header."""
    fake, recorder = _headers_call(headers)
    await client(fake, recorder).get("old", expect=nothing)
    assert recorder.of(Deprecated) == []


async def test_a_streamed_call_reports_its_deprecation() -> None:
    """Announce from a ``stream_get`` response head too."""
    fake = FakeTransport().route("GET", "/v1/old", httpx2.Response(200, content=b"a", headers={"Deprecation": "true"}))
    recorder = Recorder()
    chunks = [chunk async for chunk in client(fake, recorder).stream_get("old")]

    assert chunks == [b"a"]
    (event,) = recorder.of(Deprecated)
    assert (event.deprecated, event.deprecated_at) == (True, None)


async def test_a_client_without_observers_reads_no_header() -> None:
    """Skip the parse when nobody listens: the call still succeeds."""
    fake, _ = _headers_call({"Deprecation": "true"})
    assert await client(fake).get("old", expect=as_bytes) == b""
