# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""``examples/series_client.py``: the two codec flavors drive the same client shape and agree on success and drift."""

from __future__ import annotations

import httpx2
import pytest
import series_client as example

from headgate import ResponseParseError
from headgate.testing import FakeTransport

_PATH = "/v1/series/GDP"
_BODY = {"series_id": "GDP", "values": [1.0, 2.5]}


async def test_msgspec_and_pydantic_clients_agree() -> None:
    """Check that both flavors decode the same body to the same values, sending the key as a query parameter."""
    fakes = [FakeTransport().reply("GET", _PATH, httpx2.Response(200, json=_BODY)) for _ in range(2)]
    async with example.MsgspecClient("k", transport=fakes[0]) as msgspec_client:
        a = await msgspec_client.get_series("GDP")
    async with example.PydanticClient("k", transport=fakes[1]) as pydantic_client:
        b = await pydantic_client.get_series("GDP")

    assert (a.series_id, a.values) == (b.series_id, b.values) == ("GDP", [1.0, 2.5])
    assert all(fake.calls[0].url.params["api_key"] == "k" for fake in fakes)


@pytest.mark.parametrize("client", [example.MsgspecClient, example.PydanticClient])
async def test_both_flavors_raise_on_a_drifted_body(
    client: type[example.MsgspecClient | example.PydanticClient],
) -> None:
    """Check that both flavors raise ``ResponseParseError`` on a body that drifted from the model."""
    drifted = {"series_id": "GDP", "values": "not-a-list"}
    fake = FakeTransport().reply("GET", _PATH, httpx2.Response(200, json=drifted))
    async with client("k", transport=fake) as api:
        with pytest.raises(ResponseParseError):
            await api.get_series("GDP")
