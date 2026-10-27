# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""The SSRF guard and the credential guard apply to every way a request leaves the client.

A verb is covered by the security suite; here the other routes out are: ``stream()``, a next-page URL taken from a
response body, and a redirect. The network is a test double, so the SSRF guard runs as a request hook over a resolver
that maps test names to addresses.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx2
import msgspec
import pytest

from headgate import ForbiddenDestinationError, HttpOptions, NextUrl, SecurityOptions, StaticHeaderAuth, nothing
from headgate.msgspec import decoder
from headgate.testing import FakeTransport
from tests.integration._support import BASE, FAST, Page, client, items_of, next_of
from tests.support.clients import StubClient

if TYPE_CHECKING:
    from collections.abc import Sequence

#: The public address the API resolves to, and the private one the attacker's name resolves to.
_ADDRESSES = {"api.example.test": "93.184.216.34", "cdn.example.test": "93.184.216.35", "internal.test": "10.0.0.7"}

_INTERNAL = "https://internal.test/admin"


async def _resolve(host: str, _port: int) -> Sequence[str]:
    """Resolve *host* from :data:`_ADDRESSES`."""
    return [_ADDRESSES[host]]


_GUARDED = HttpOptions(security=SecurityOptions(allow_private_hosts=False, resolver=_resolve))


async def test_a_stream_to_a_private_address_is_refused() -> None:
    """Refuse to open a stream to a host that resolves to a private address, sending nothing."""
    fake = FakeTransport().reply("GET", "/admin", 200)
    built = client(fake, http=_GUARDED)

    with pytest.raises(ForbiddenDestinationError, match=r"10\.0\.0\.7.*private"):
        async with built.stream("GET", _INTERNAL):
            pass

    assert fake.calls == ()


async def test_a_stream_through_the_pinning_transport_is_refused() -> None:
    """Refuse a stream on the default network transport, which vets the address it connects to."""
    async with StubClient(base_url=BASE, rate=FAST, http=_GUARDED) as built:
        with pytest.raises(ForbiddenDestinationError, match=r"10\.0\.0\.7.*private"):
            async with built.stream("GET", _INTERNAL):
                pass


async def test_a_next_page_url_to_a_private_address_is_refused() -> None:
    """Refuse the page a response points at when its host resolves to a private address, after the first page."""
    first = httpx2.Response(200, content=msgspec.json.encode(Page(data=[1], next=_INTERNAL)))
    fake = FakeTransport().reply("GET", "/v1/items", first).reply("GET", "/admin", 200)
    built = client(fake, http=_GUARDED)
    items = aiter(built.paginate("items", expect=decoder(Page), pages=NextUrl(items=items_of, next=next_of)))

    assert await anext(items) == 1
    with pytest.raises(ForbiddenDestinationError):
        await anext(items)
    assert len(fake.calls) == 1


async def test_a_redirect_to_a_private_address_is_refused() -> None:
    """Refuse a redirect hop whose host resolves to a private address, before it is sent."""
    moved = httpx2.Response(302, headers={"Location": _INTERNAL})
    fake = FakeTransport().reply("GET", "/v1/x", moved).reply("GET", "/admin", 200)
    built = client(fake, http=_GUARDED)

    with pytest.raises(ForbiddenDestinationError):
        await built.get("x", expect=nothing)

    assert len(fake.calls) == 1


async def test_a_stream_to_another_origin_carries_no_credential() -> None:
    """Open a stream to another origin without the auth's header, and to the API's own origin with it."""
    fake = FakeTransport().reply("GET", "/v1/file", 200).reply("GET", "/file", 200)
    built = client(fake, auth=StaticHeaderAuth({"X-Api-Key": "secret"}))

    async with built.stream("GET", "file"):
        pass
    async with built.stream("GET", "https://cdn.example.test/file"):
        pass

    own, foreign = fake.calls
    assert own.headers.get("X-Api-Key") == "secret"
    assert "X-Api-Key" not in foreign.headers
