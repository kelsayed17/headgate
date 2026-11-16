# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""One client in both codec flavors: the same shape, only the import and the model's base class differ."""

from __future__ import annotations

from typing import Self

import msgspec
from pydantic import BaseModel

from headgate import Client, QueryParamAuth, Rate
from headgate import msgspec as msgspec_flavor
from headgate import pydantic as pydantic_flavor

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


MSGSPEC_SERIES = msgspec_flavor.reader(MsgspecSeries)
PYDANTIC_SERIES = pydantic_flavor.reader(PydanticSeries)


class MsgspecClient:
    """The series API, decoding with the msgspec flavor."""

    def __init__(self, api_key: str) -> None:
        """Send *api_key* as the ``api_key`` query parameter."""
        self._http = Client(
            base_url=_BASE_URL,
            rate=_RATE,
            auth=QueryParamAuth({"api_key": api_key}),
        )

    async def __aenter__(self) -> Self:
        """Return the client; leaving the block closes it."""
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        """Close the client."""
        await self.aclose()

    async def aclose(self) -> None:
        """Close the connection pool."""
        await self._http.aclose()

    async def get_series(self, series_id: str) -> MsgspecSeries:
        """Return the series *series_id*."""
        return await self._http.get(t"series/{series_id}", expect=MSGSPEC_SERIES)


class PydanticClient:
    """The series API, decoding with the pydantic flavor."""

    def __init__(self, api_key: str) -> None:
        """Send *api_key* as the ``api_key`` query parameter."""
        self._http = Client(
            base_url=_BASE_URL,
            rate=_RATE,
            auth=QueryParamAuth({"api_key": api_key}),
        )

    async def __aenter__(self) -> Self:
        """Return the client; leaving the block closes it."""
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        """Close the client."""
        await self.aclose()

    async def aclose(self) -> None:
        """Close the connection pool."""
        await self._http.aclose()

    async def get_series(self, series_id: str) -> PydanticSeries:
        """Return the series *series_id*."""
        return await self._http.get(t"series/{series_id}", expect=PYDANTIC_SERIES)
