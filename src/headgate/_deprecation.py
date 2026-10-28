# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""Endpoint deprecation and sunset announcements: the ``Deprecation`` (RFC 9745) and ``Sunset`` (RFC 8594) headers.

A provider retiring an endpoint says so on every response from it. :func:`deprecation_of` reads what a response
announces into a :class:`~headgate.events.Deprecated` event, and :func:`announce_deprecation` emits it to the call's
observers, once per call, from the final response's head.

Both formats of ``Deprecation`` are read: RFC 9745's structured-field date (``@1688169599``, seconds since the epoch)
and the older drafts' ``true`` or HTTP-date. ``Sunset`` is an HTTP-date. A header whose value is neither is ignored, as
RFC 9745 asks of an invalid one. The ``Link`` targets with ``rel="deprecation"`` and ``rel="sunset"`` are reported
when either header is, resolved against the request URL and with any credential in them redacted.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING, Final

from headgate._core import Deprecated
from headgate._url import sanitize_url
from headgate.pagination._link import parse_link_header

if TYPE_CHECKING:
    import httpx2

    from headgate._core import CallContext, CallInfo

__all__ = ["announce_deprecation", "deprecation_of", "parse_deprecation", "parse_http_date"]

#: An RFC 8941 Date: ``@`` and an Integer of at most 15 digits.
_SF_DATE: Final = re.compile(r"@(-?[0-9]{1,15})")

#: What parsing a date can raise: a value ``parsedate_to_datetime`` rejects, or a timestamp past ``datetime``'s range.
_DATE_ERRORS = (TypeError, ValueError, OverflowError, OSError)


def parse_http_date(value: str) -> datetime | None:
    """Return the HTTP-date *value* as an aware UTC ``datetime``, or ``None`` when it is not one."""
    try:
        when = parsedate_to_datetime(value.strip())
    except _DATE_ERRORS:
        return None
    return when.replace(tzinfo=UTC) if when.tzinfo is None else when.astimezone(UTC)


def parse_deprecation(value: str) -> tuple[bool, datetime | None]:
    """Return whether a ``Deprecation`` header *value* is valid, and the date it names.

    ``@<seconds>`` (RFC 9745) and an HTTP-date name a date; ``true`` (the older drafts) names none. Anything else is
    invalid: ``(False, None)``.
    """
    text = value.strip()
    if (match := _SF_DATE.fullmatch(text)) is not None:
        try:
            return True, datetime.fromtimestamp(int(match[1]), UTC)
        except _DATE_ERRORS:
            return False, None
    if text.lower() == "true":
        return True, None
    when = parse_http_date(text)
    return when is not None, when


def _link(response: httpx2.Response, rel: str) -> str | None:
    """Return the target of *response*'s first ``Link`` with relation *rel*, resolved and sanitized, or ``None``.

    The ``Link`` fields are read as RFC 8288 writes them, by the parser :class:`~headgate.pagination.LinkHeader`
    follows: a link's ``rel`` holds relation types separated by spaces, compared without regard to case.
    """
    for value in response.headers.get_list("link"):
        for link in parse_link_header(value):
            if rel in link.rels:
                return sanitize_url(str(response.request.url.join(link.target)))
    return None


def deprecation_of(response: httpx2.Response, call: CallInfo) -> Deprecated | None:
    """Return the :class:`~headgate.events.Deprecated` event *response* announces for *call*, or ``None``.

    ``None`` unless the response carries a valid ``Deprecation`` or ``Sunset`` header.
    """
    deprecation = response.headers.get("Deprecation")
    sunset = response.headers.get("Sunset")
    if deprecation is None and sunset is None:
        return None
    deprecated, deprecated_at = (False, None) if deprecation is None else parse_deprecation(deprecation)
    sunset_at = None if sunset is None else parse_http_date(sunset)
    if not deprecated and sunset_at is None:
        return None
    return Deprecated(
        call=call,
        deprecated=deprecated,
        deprecated_at=deprecated_at,
        sunset_at=sunset_at,
        link=_link(response, "deprecation"),
        sunset_link=_link(response, "sunset"),
    )


def announce_deprecation(context: CallContext, response: httpx2.Response) -> None:
    """Emit the :class:`~headgate.events.Deprecated` event *response* announces, when the call has observers."""
    if context.observed and (event := deprecation_of(response, context.info)) is not None:
        context.emit(event)
