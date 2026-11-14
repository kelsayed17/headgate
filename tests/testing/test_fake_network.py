# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""``fake_network``: every client built in its block sends on the test's transport, the rest of its stack its own."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from headgate import Client, HttpOptions, Rate, nothing
from headgate.curl import AsyncCurlTransport
from headgate.testing import FakeTransport, budget_for, fake_network

if TYPE_CHECKING:
    import httpx2

_RATE: Final = Rate.per_second(50)


class _Sdk:
    """An SDK that holds a ``Client`` and fixes its transport, as one built for production would."""

    def __init__(self, transport: httpx2.AsyncBaseTransport | None = None) -> None:
        """Hold a client on *transport*, else on curl."""
        self.http = Client(
            base_url="https://api.example.test/v1",
            rate=_RATE,
            http=HttpOptions(transport=AsyncCurlTransport(impersonate="chrome") if transport is None else transport),
        )

    async def ping(self) -> None:
        """Make one call."""
        await self.http.get("ping", expect=nothing)


async def test_an_sdk_built_in_the_block_sends_on_the_fake() -> None:
    """Send on the fake, though the SDK fixed a curl transport of its own, and yield the fake it was given."""
    fake = FakeTransport().reply("GET", "/v1/ping", 200)
    with fake_network(fake) as yielded:
        sdk = _Sdk()
    await sdk.ping()

    assert yielded is fake
    assert [call.url.path for call in fake.calls] == ["/v1/ping"]


async def test_an_sdk_built_outside_the_block_keeps_its_own_transport() -> None:
    """Leave a client built after the block on the transport it chose."""
    own, fake = FakeTransport().reply("GET", "/v1/ping", 200), FakeTransport()
    with fake_network(fake):
        pass
    sdk = _Sdk(own)
    await sdk.ping()

    assert (len(own.calls), len(fake.calls)) == (1, 0)


async def test_the_sdks_retries_and_rate_budget_run_over_the_fake() -> None:
    """Replace the network only: the client still retries a 503 and spends its own rate budget."""
    fake = FakeTransport().reply("GET", "/v1/ping", 503, 200)
    with fake_network(fake):
        sdk = _Sdk()
    await sdk.ping()

    assert len(fake.calls) == len(("503", "200"))
    assert budget_for(sdk.http).rate == _RATE
