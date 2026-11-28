# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""A declaration the call could never act on is refused at the call, before anything is sent.

A redirect status declared on an ``Expect`` is never seen by a call that follows redirects: the client follows the
3xx and hands the target's body to the success reader. A ``cache_ttl=`` on a client without a cache sets the lifetime
of nothing.
"""

from __future__ import annotations

from typing import Final

import httpx2
import pytest

from headgate import Client, Expect, HttpOptions, Reader, as_json, nothing
from headgate.pagination import LinkHeader
from headgate.testing import FakeTransport

_BASE: Final = "https://api.example.test/"

_LOCATION: Final = Reader(lambda response: response.headers["Location"], name="location")

_MOVED: Final = Expect(as_json).on(302, _LOCATION)


def _client(fake: FakeTransport) -> Client:
    return Client(base_url=_BASE, http=HttpOptions(transport=fake))


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
async def test_a_redirect_declared_on_a_following_call_is_refused(status: int) -> None:
    """Refuse a declared redirect on a call that follows redirects, naming it and what to pass, sending nothing."""
    fake = FakeTransport()
    async with _client(fake) as client:
        with pytest.raises(ValueError, match=rf"^{status} is declared on this Expect, but the call follows redirects"):
            await client.get("x", expect=Expect(as_json).on(status, _LOCATION))
        with pytest.raises(ValueError, match=r"pass follow_redirects=False"):
            await client.get("x", expect=Expect(as_json).on(status, _LOCATION))
    assert not fake.calls


async def test_a_redirect_declared_on_a_call_that_does_not_follow_is_read() -> None:
    """Read the declared 302 with its reader on a call, or a view, that does not follow redirects."""
    fake = FakeTransport().reply("GET", "/x", httpx2.Response(302, headers={"Location": "/y"}))
    async with _client(fake) as client:
        assert await client.get("x", expect=_MOVED, follow_redirects=False) == "/y"
        assert await client.with_options(follow_redirects=False).get("x", expect=_MOVED) == "/y"


async def test_a_status_that_is_not_followed_may_be_declared() -> None:
    """Accept a declared 304, which no client follows, on a call that follows redirects."""
    fake = FakeTransport().reply("GET", "/x", 304)
    async with _client(fake) as client:
        assert await client.get("x", expect=Expect(as_json).on(304, nothing)) is None


async def test_a_walk_refuses_a_page_reader_declaring_a_redirect() -> None:
    """Refuse a pager whose reader declares a redirect on a walk that follows redirects, where the walk is made."""
    fake = FakeTransport()
    pager = LinkHeader(
        Expect(Reader(lambda r: [r.status_code], name="statuses")).on(307, Reader(lambda r: [r.status_code])),
        items=list,
    )
    async with _client(fake) as client:
        with pytest.raises(ValueError, match=r"^307 is declared on this Expect, but the call follows redirects"):
            client.paginate("x", pager=pager)
    assert not fake.calls


async def test_a_cache_lifetime_on_a_client_without_a_cache_is_refused() -> None:
    """Refuse ``cache_ttl=`` on a client with no cache, which would set the lifetime of nothing."""
    fake = FakeTransport()
    async with _client(fake) as client:
        with pytest.raises(ValueError, match=r"^cache_ttl= sets a cached response's lifetime, but the client of"):
            await client.get("x", expect=nothing, cache_ttl=60.0)
    assert not fake.calls
