"""The two codec flavors drive the same client shape and agree on success and on drift."""

from __future__ import annotations

import sys
from collections.abc import Mapping
from pathlib import Path

import httpx2
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "examples"))
import fred_style_client as example

from headgate import ResponseParseError


def _transport(body: Mapping[str, object]) -> httpx2.MockTransport:
    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.url.params["api_key"] == "k"
        assert request.url.params["id"] == "GDP"
        return httpx2.Response(200, json=body)

    return httpx2.MockTransport(handler)


async def test_msgspec_and_pydantic_clients_agree() -> None:
    body = {"series_id": "GDP", "values": [1.0, 2.5]}
    a = await example.MsgspecClient("k", transport=_transport(body)).get_series("GDP")
    b = await example.PydanticClient("k", transport=_transport(body)).get_series("GDP")
    assert (a.series_id, a.values) == (b.series_id, b.values) == ("GDP", [1.0, 2.5])


@pytest.mark.parametrize("client", [example.MsgspecClient, example.PydanticClient])
async def test_both_flavors_raise_on_a_drifted_body(
    client: type[example.MsgspecClient | example.PydanticClient],
) -> None:
    drifted = {"series_id": "GDP", "values": "not-a-list"}
    with pytest.raises(ResponseParseError):
        await client("k", transport=_transport(drifted)).get_series("GDP")
