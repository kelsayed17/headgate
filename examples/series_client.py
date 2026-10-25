# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""One client in both codec flavors: the same shape, only the import and the model's base class differ."""

from __future__ import annotations

from typing import TYPE_CHECKING

import msgspec
from pydantic import BaseModel

from headgate import AsyncBaseClient, HttpOptions, QueryParamAuth, Rate
from headgate import msgspec as msgspec_flavor
from headgate import pydantic as pydantic_flavor

if TYPE_CHECKING:
    import httpx2

_BASE_URL = "https://api.series.example/v1"
_RATE = Rate.per_second(5)


class MsgspecSeries(msgspec.Struct, frozen=True):
    """A data series, decoded by msgspec."""

    series_id: str
    values: list[float]


class PydanticSeries(BaseModel, frozen=True):
    """A data series, decoded by pydantic."""

    series_id: str
    values: list[float]


MSGSPEC_SERIES = msgspec_flavor.decoder(MsgspecSeries)
PYDANTIC_SERIES = pydantic_flavor.decoder(PydanticSeries)


class MsgspecClient(AsyncBaseClient):
    """The series API, decoding with the msgspec flavor."""

    def __init__(self, api_key: str, *, transport: httpx2.AsyncBaseTransport | None = None) -> None:
        """Send *api_key* as the ``api_key`` query parameter; *transport* replaces the network, as a test does."""
        super().__init__(
            base_url=_BASE_URL,
            rate=_RATE,
            auth=QueryParamAuth({"api_key": api_key}),
            http=HttpOptions(transport=transport),
        )

    async def get_series(self, series_id: str) -> MsgspecSeries:
        """Return the series *series_id*."""
        return await self.get(t"series/{series_id}", expect=MSGSPEC_SERIES)


class PydanticClient(AsyncBaseClient):
    """The series API, decoding with the pydantic flavor."""

    def __init__(self, api_key: str, *, transport: httpx2.AsyncBaseTransport | None = None) -> None:
        """Send *api_key* as the ``api_key`` query parameter; *transport* replaces the network, as a test does."""
        super().__init__(
            base_url=_BASE_URL,
            rate=_RATE,
            auth=QueryParamAuth({"api_key": api_key}),
            http=HttpOptions(transport=transport),
        )

    async def get_series(self, series_id: str) -> PydanticSeries:
        """Return the series *series_id*."""
        return await self.get(t"series/{series_id}", expect=PYDANTIC_SERIES)
