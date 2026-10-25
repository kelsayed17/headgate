# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""A minimal client in both codec flavors: same shape, only the import and the model type differ."""

from __future__ import annotations

from typing import TYPE_CHECKING

import msgspec
from pydantic import BaseModel

from headgate import AsyncBaseClient, QueryParamAuth, Rate
from headgate import msgspec as msgspec_flavor
from headgate import pydantic as pydantic_flavor

if TYPE_CHECKING:
    import httpx2


class MsgspecSeries(msgspec.Struct, frozen=True):
    series_id: str
    values: list[float]


class PydanticSeries(BaseModel, frozen=True):
    series_id: str
    values: list[float]


class MsgspecClient(AsyncBaseClient):
    def __init__(self, api_key: str, *, transport: httpx2.AsyncBaseTransport | None = None) -> None:
        super().__init__(
            base_url="https://api.example.test/v1",
            rate=Rate.per_second(5),
            auth=QueryParamAuth({"api_key": api_key}),
            transport=transport,
        )

    async def get_series(self, series_id: str) -> MsgspecSeries:
        return await self.get("series", expect=msgspec_flavor.decoder(MsgspecSeries), params={"id": series_id})


class PydanticClient(AsyncBaseClient):
    def __init__(self, api_key: str, *, transport: httpx2.AsyncBaseTransport | None = None) -> None:
        super().__init__(
            base_url="https://api.example.test/v1",
            rate=Rate.per_second(5),
            auth=QueryParamAuth({"api_key": api_key}),
            transport=transport,
        )

    async def get_series(self, series_id: str) -> PydanticSeries:
        return await self.get("series", expect=pydantic_flavor.decoder(PydanticSeries), params={"id": series_id})
