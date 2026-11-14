# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""``fake_network``: every client built in its block sends on the test's transport, the rest of its stack its own."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

import pytest

from headgate import Client, HttpOptions, Rate, SecurityOptions, nothing
from headgate.curl import AsyncCurlTransport
from headgate.errors import ForbiddenDestinationError
from headgate.testing import FakeTransport, TestClock, fake_network

if TYPE_CHECKING:
    import httpx2

_RATE: Final = Rate.per_second(50)


class _Sdk:
    """An SDK that holds a ``Client`` and fixes its transport, as one built for production would."""

    def __init__(self, transport: httpx2.AsyncBaseTransport | None = None, rate: Rate = _RATE) -> None:
        """Hold a client on *transport*, else on curl, paced at *rate*."""
        self.http = Client(
            base_url="https://api.example.test/v1",
            rate=rate,
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


@pytest.mark.transport_policy
async def test_the_sdks_retries_and_rate_budget_run_over_the_fake() -> None:
    """Replace the network only: the client still retries a 503, and paces its sends at its own rate."""
    fake = FakeTransport().reply("GET", "/v1/ping", 503, 200)
    clock = TestClock(random=lambda: 0.0)
    with fake_network(fake), clock.install():
        sdk = _Sdk(rate=Rate.per_second(1))
    await sdk.ping()
    await sdk.ping()

    assert len(fake.calls) == len(("503", "200", "200"))
    assert sum(clock.sleeps) >= 1.0, "the sends waited their turns in the budget"


async def test_the_ssrf_guard_still_refuses_a_private_address() -> None:
    """Keep the SSRF guard of a client that blocks private hosts: the fake replaces the network, not the guard."""
    fake = FakeTransport().reply("GET", "/x", 200)
    with fake_network(fake):
        client = Client(
            base_url="http://127.0.0.1",
            rate=_RATE,
            http=HttpOptions(security=SecurityOptions(block_private_hosts=True)),
        )

    with pytest.raises(ForbiddenDestinationError):
        await client.get("x", expect=nothing)
    assert not fake.calls
