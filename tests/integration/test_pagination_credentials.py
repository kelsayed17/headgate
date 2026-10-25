# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""Pagination hands an absolute next-page URL to the client, and the credential guard decides what it carries.

A next URL on another origin (a CDN, a signed download host) is fetched without any credential the client's auth
stamps, whichever auth it is; the same URL on a trusted origin keeps them.
"""

from __future__ import annotations

import httpx2
import msgspec
import pytest

from headgate import HttpOptions, NextUrl
from headgate.msgspec import decoder
from headgate.testing import FakeTransport
from tests.integration._support import Page, carried, client, items_of, next_of, real_auths

_FOREIGN = "https://cdn.other.test"
_PAGES = NextUrl(items=items_of, next=next_of)
_AUTHS = real_auths()


def _pages(next_url: str) -> FakeTransport:
    """Return a network serving a first page that links to *next_url*, and the page there."""
    first = httpx2.Response(200, content=msgspec.json.encode(Page(data=[1], next=next_url)))
    second = httpx2.Response(200, content=msgspec.json.encode(Page(data=[2])))
    return FakeTransport().route("GET", "/v1/items", first).route("GET", "/v1/page2", second)


@pytest.mark.parametrize("name", sorted(_AUTHS))
async def test_a_next_url_on_another_origin_carries_no_credential(name: str) -> None:
    """Send the second page to the foreign origin with none of the auth's credentials, and the first page with all."""
    auth = _AUTHS[name]
    fake = _pages(f"{_FOREIGN}/v1/page2")
    built = client(fake, auth=auth)

    items = [item async for item in built.paginate("items", expect=decoder(Page), pages=_PAGES)]

    first, second = fake.calls
    assert items == [1, 2]
    assert second.url.host == "cdn.other.test"
    assert carried(first, auth), "the API's own origin gets the credential"
    assert not carried(second, auth)


@pytest.mark.parametrize("name", sorted(_AUTHS))
async def test_a_next_url_on_a_trusted_origin_keeps_the_credentials(name: str) -> None:
    """Send the credentials to a next-page origin the client trusts, exactly as to its own."""
    auth = _AUTHS[name]
    fake = _pages(f"{_FOREIGN}/v1/page2")
    built = client(fake, auth=auth, http=HttpOptions(trusted_origins=frozenset({_FOREIGN})))

    _ = [item async for item in built.paginate("items", expect=decoder(Page), pages=_PAGES)]

    first, second = fake.calls
    assert carried(second, auth) == carried(first, auth) != set()
