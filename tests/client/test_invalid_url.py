# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""A malformed URL raises as a ``ClientError``, not as httpx2's ``InvalidURL``.

httpx2's ``InvalidURL`` is not an ``HTTPError``, so the client maps it explicitly; without that it would escape the
``ClientError`` tree every caller catches. It is also not a host failure, so it must not reach the per-host circuit
breaker.
"""

from __future__ import annotations

import pytest

from headgate._client import AsyncBaseClient
from headgate._responses import as_bytes
from headgate.errors import ClientError
from headgate.ratelimit import Rate
from tests.support.breakers import failures


class _Client(AsyncBaseClient):
    """A client against ``example.test``."""

    def __init__(self) -> None:
        """Build with the defaults."""
        super().__init__(base_url="https://example.test", rate=Rate.per_second(1000))


async def test_a_malformed_url_is_a_client_error_and_not_a_host_failure() -> None:
    """Check that a URL httpx2 cannot parse raises ``ClientError`` and charges no breaker."""
    client = _Client()
    try:
        with pytest.raises(ClientError, match="invalid URL") as raised:
            await client.get("a\x00b", expect=as_bytes)
    finally:
        await client.aclose()
    assert failures("example.test") == 0, "a malformed URL is not a host failure"
    assert str(raised.value).startswith("[_Client] invalid URL a%00b: Invalid non-printable ASCII character in URL")
    assert (raised.value.method, raised.value.url, raised.value.source) == ("GET", "a%00b", "_Client")


async def test_a_control_character_in_a_url_cannot_forge_a_log_line() -> None:
    """Percent-encode a line break in a URL a message names, so the message stays one line."""
    client = _Client()
    try:
        with pytest.raises(ClientError) as raised:
            await client.get("a\nERROR forged\r\x7f", expect=as_bytes)
    finally:
        await client.aclose()
    assert raised.value.url == "a%0AERROR forged%0D%7F"
    assert "\n" not in str(raised.value)
