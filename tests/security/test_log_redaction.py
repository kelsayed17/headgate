# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""httpx2's own log line names each request's URL, so it is redacted like every URL headgate reports.

httpx2 logs ``HTTP Request: GET <url> ...`` at INFO for every response. A key in the query (a ``QueryParamAuth``'s, a
``SigningAuth``'s signature) would reach any log that records INFO, so headgate filters the ``httpx2`` logger.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import httpx2

from headgate import QueryParamAuth
from tests.security._web import BASE, Web
from tests.support.responses import RESPONSE

if TYPE_CHECKING:
    import pytest


async def test_httpx2s_request_line_logs_the_url_with_its_secrets_redacted(caplog: pytest.LogCaptureFixture) -> None:
    """Log the request line with the key's value replaced, and every other part of the URL as it was sent."""
    web = Web()
    with caplog.at_level(logging.INFO, logger="httpx2"):
        async with web.client(auth=QueryParamAuth({"api_key": "S3CR3T"})) as client:
            await client.get("series", params={"page": "2"}, expect=RESPONSE)

    (line,) = (record.getMessage() for record in caplog.records if record.name == "httpx2")
    assert "S3CR3T" not in line
    assert line.startswith(f"HTTP Request: GET {BASE}/series?page=2&api_key=")


def test_a_record_without_a_url_is_left_alone(caplog: pytest.LogCaptureFixture) -> None:
    """Pass a record whose arguments hold no URL, or that has none, through unchanged."""
    logger = logging.getLogger("httpx2")
    with caplog.at_level(logging.INFO, logger="httpx2"):
        logger.info("plain %s", "text")
        logger.info("no arguments")
        logger.info("a mapping %(key)s", {"key": "value"})
        logger.info("a url %s", httpx2.URL("https://api.example/x?token=t0k"))

    assert [record.getMessage() for record in caplog.records] == [
        "plain text",
        "no arguments",
        "a mapping value",
        "a url https://api.example/x?token=%2A%2A%2A",
    ]
