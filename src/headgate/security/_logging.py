# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""httpx2's own log line, redacted: every URL it logs has its credentials replaced, as in every message headgate writes.

httpx2 logs ``HTTP Request: GET <url> "HTTP/1.1 200 OK"`` at INFO for each response, with the URL as sent: a
``QueryParamAuth`` key or a ``SigningAuth`` query signature in clear, for any application that records INFO. Importing
headgate adds :class:`RedactedUrls` to the ``httpx2`` logger, so the URL is logged through
:func:`~headgate._url.sanitize_url`. It only ever replaces values with ``***``, so it changes nothing else a log shows.

**The filter is process-wide.** A logger is global, so it applies to every httpx2 client in the process, not only
headgate's: an ``httpx2.AsyncClient`` the application builds itself logs its URLs redacted too.
"""

from __future__ import annotations

import logging
from typing import override

import httpx2

from headgate._url import sanitize_url

__all__ = ["RedactedUrls", "redact_httpx2_log"]


class RedactedUrls(logging.Filter):
    """A filter replacing each ``httpx2.URL`` among a record's arguments with its redacted text."""

    @override
    def filter(self, record: logging.LogRecord) -> bool:
        """Redact the record's URL arguments in place; never drop the record."""
        if isinstance(record.args, tuple):
            record.args = tuple(sanitize_url(str(arg)) if isinstance(arg, httpx2.URL) else arg for arg in record.args)
        return True


def redact_httpx2_log() -> None:
    """Add :class:`RedactedUrls` to the ``httpx2`` logger, for every httpx2 client in the process.

    Importing :mod:`headgate.security` calls it once.
    """
    logging.getLogger("httpx2").addFilter(RedactedUrls())
