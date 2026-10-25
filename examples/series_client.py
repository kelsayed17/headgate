# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""A minimal client in both codec flavors: same shape, only the import and the model type differ."""

from __future__ import annotations

from typing import TYPE_CHECKING

import msgspec
from pydantic import BaseModel

from headgate import AsyncBaseClient, HttpOptions, QueryParamAuth, Rate
from headgate import msgspec as msgspec_flavor
from headgate import pydantic as pydantic_flavor

if TYPE_CHECKING:
    import httpx2

_BASE_URL = "https://api.example.test/v1"
_RATE = Rate.per_second(5)


class MsgspecSeries(msgspec.Struct, frozen=True):
    """A data series, decoded by msgspec."""

    series_id: str
    values: list[float]


class PydanticSeries(BaseModel, frozen=True):
    """A data series, decoded by pydantic."""

    series_id: str
    values: list[float]


class MsgspecClient(AsyncBaseClient):
    """The series API, decoding with the msgspec flavor."""

    def __init__(self, api_key: str, *, transport: httpx2.AsyncBaseTransport | None = None) -> None:
        """Authenticate with *api_key* as a query parameter; *transport* replaces the network, as in a test."""
        super().__init__(
            base_url=_BASE_URL,
            rate=_RATE,
            auth=QueryParamAuth({"api_key": api_key}),
            http=HttpOptions(transport=transport),
        )

    async def get_series(self, series_id: str) -> MsgspecSeries:
        """Return the series *series_id*."""
        return await self.get("series", expect=msgspec_flavor.decoder(MsgspecSeries), params={"id": series_id})


class PydanticClient(AsyncBaseClient):
    """The series API, decoding with the pydantic flavor."""

    def __init__(self, api_key: str, *, transport: httpx2.AsyncBaseTransport | None = None) -> None:
        """Authenticate with *api_key* as a query parameter; *transport* replaces the network, as in a test."""
        super().__init__(
            base_url=_BASE_URL,
            rate=_RATE,
            auth=QueryParamAuth({"api_key": api_key}),
            http=HttpOptions(transport=transport),
        )

    async def get_series(self, series_id: str) -> PydanticSeries:
        """Return the series *series_id*."""
        return await self.get("series", expect=pydantic_flavor.decoder(PydanticSeries), params={"id": series_id})
