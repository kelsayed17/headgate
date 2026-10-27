# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""The README's quickstart: a complete client for a small data-catalog API, and code that uses it.

The README's Python blocks are excerpts of the modules in ``examples/`` and ``tests/examples/``, which
``tests/examples/test_docs.py`` checks; ``tests/examples/test_quickstart.py`` runs this one against a fake network.
"""

from http import HTTPStatus
from typing import TYPE_CHECKING

import msgspec

from headgate import AsyncBaseClient, BearerAuth, Cursor, Expect, HttpOptions, Rate, nothing
from headgate.msgspec import decoder

if TYPE_CHECKING:
    import httpx2

    from headgate import Paginated


class Series(msgspec.Struct, frozen=True):
    """A data series: its id, title and observations."""

    id: str
    title: str
    values: list[float]


class Release(msgspec.Struct, frozen=True):
    """A scheduled data release."""

    id: int
    name: str


class ReleasePage(msgspec.Struct, frozen=True):
    """One page of releases, and the cursor of the next page (``None`` on the last)."""

    releases: list[Release]
    next_cursor: str | None = None


def releases_on(page: ReleasePage) -> list[Release]:
    """Return the releases on *page*."""
    return page.releases


def cursor_after(page: ReleasePage) -> str | None:
    """Return the cursor of the page after *page*."""
    return page.next_cursor


# Readers and pagers are values: build them once, at import, where a bad declaration fails.
SERIES = decoder(Series)
SERIES_OR_NONE = Expect(SERIES).on(HTTPStatus.NOT_FOUND, nothing)  # the API documents 404 as "no such series"
RELEASE_PAGES = Cursor(items=releases_on, next=cursor_after, param="cursor")


class CatalogClient(AsyncBaseClient):
    """The catalog API: one method per endpoint, each one line."""

    def __init__(self, api_key: str, *, transport: httpx2.AsyncBaseTransport | None = None) -> None:
        """Authenticate with *api_key*; *transport* replaces the network, as a test does."""
        super().__init__(
            base_url="https://api.catalog.example/v1",
            rate=Rate.per_second(10),
            auth=BearerAuth(api_key),
            http=HttpOptions(transport=transport),
        )

    async def get_series(self, series_id: str) -> Series:
        """Return the series *series_id*; a 404, undeclared here, raises ``UpstreamStatusError``."""
        return await self.get(t"series/{series_id}", expect=SERIES)

    async def find_series(self, series_id: str) -> Series | None:
        """Return the series *series_id*, or ``None`` when the API answers 404."""
        return await self.get(t"series/{series_id}", expect=SERIES_OR_NONE)

    def releases(self) -> Paginated[ReleasePage, Release]:
        """Return every release, fetched a page at a time as the iteration reaches it."""
        return self.paginate("releases", expect=decoder(ReleasePage), pages=RELEASE_PAGES)


async def main(api_key: str) -> tuple[float, Series | None, list[str]]:
    """Read one series, look up one that may not exist, and list every release."""
    async with CatalogClient(api_key) as catalog:
        gdp = await catalog.get_series("GDP")
        retired = await catalog.find_series("RETIRED")  # None: the API answered 404
        names = [release.name async for release in catalog.releases()]
    return gdp.values[-1], retired, names
