# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""httpx2's own log line names each request's URL, so it is redacted like every URL headgate reports.

httpx2 logs ``HTTP Request: GET <url> ...`` at INFO for every response. A key in the query (a ``QueryParamAuth``'s, a
``SigningAuth``'s signature) would reach any log that records INFO, so headgate filters the ``httpx2`` logger.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, ClassVar, override

import httpx2
import pytest

from headgate import Auth, Next, QueryParamAuth
from tests.security._web import BASE, Web
from tests.support.responses import RESPONSE

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


#: The key each stamper below sends, which no log line may show.
_KEY = "S3CR3T"


class _RelayTicket:
    """A middleware stamping a query credential whose name nothing else in the suite declares."""

    credential_headers = frozenset[str]()
    credential_params = frozenset({"relay_ticket"})

    async def __call__(self, request: httpx2.Request, call_next: Next) -> httpx2.Response:
        """Add the ticket, then send."""
        request.url = request.url.copy_merge_params({"relay_ticket": _KEY})
        return await call_next(request)


class _PartnerSignature(Auth):
    """An auth of its own stamping a query credential whose name nothing else in the suite declares."""

    credential_headers: ClassVar = frozenset[str]()
    credential_params: ClassVar = frozenset({"partner_mac"})

    @override
    def credential_identity(self) -> bytes:
        return _KEY.encode()

    @override
    async def async_auth_flow(self, request: httpx2.Request) -> AsyncGenerator[httpx2.Request, httpx2.Response]:
        request.url = request.url.copy_merge_params({"partner_mac": _KEY})
        yield request


@pytest.mark.parametrize(
    ("auth", "middleware"), [(None, (_RelayTicket(),)), (_PartnerSignature(), ())], ids=["middleware", "auth"]
)
async def test_a_declared_query_credential_is_redacted_from_the_request_line(
    caplog: pytest.LogCaptureFixture, auth: Auth | None, middleware: tuple[_RelayTicket, ...]
) -> None:
    """Redact a query parameter a middleware or an auth of the caller's declares, as a ``QueryParamAuth``'s is."""
    web = Web()
    with caplog.at_level(logging.INFO, logger="httpx2"):
        async with web.client(auth=auth, middleware=middleware) as client:
            await client.get("series", expect=RESPONSE)

    (line,) = (record.getMessage() for record in caplog.records if record.name == "httpx2")
    assert web.sent[0].url.params.get("relay_ticket" if middleware else "partner_mac") == _KEY, "it was sent"
    assert _KEY not in line


async def test_httpx2s_request_line_logs_the_url_with_its_secrets_redacted(caplog: pytest.LogCaptureFixture) -> None:
    """Log the request line with the key's value replaced, and every other part of the URL as it was sent.

    A real request through httpx2's own logger, so a renamed logger leaves no record and fails the test.
    """
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
        "a url https://api.example/x?token=***",
    ]
