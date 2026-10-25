# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""The lazy sequence of pages and items :meth:`AsyncBaseClient.paginate <headgate.AsyncBaseClient.paginate>` returns."""

from __future__ import annotations

from typing import TYPE_CHECKING, final

from headgate._failures import loggable_url
from headgate.errors import ClientError

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

    import httpx2

    from headgate.pagination._pager import Pager, PageRequest

__all__ = ["Paginated"]

type Fetch[P] = Callable[[PageRequest], Awaitable[tuple[P, httpx2.Response]]]
"""Fetches one page through the client, returning what the call's reader read and the response it read it from."""


@final
class Paginated[P, I]:
    """The pages of a paginated endpoint and the items on them, fetched one page at a time as iteration reaches them.

    ``async for item in paginated`` yields every item; :meth:`pages` yields the pages themselves. Nothing is fetched
    until iteration starts, no page is held after the iteration moves past it, and each iteration starts again from
    the first page. Stopping early fetches nothing more.

    A page that leads back to a request already made (a cursor or next URL that repeats) raises ``ClientError``
    instead of looping forever; the requests made are remembered for that, one small key per page.
    """

    __slots__ = ("_fetch", "_first", "_pager", "_source")

    def __init__(self, fetch: Fetch[P], pager: Pager[P, I], first: PageRequest, *, source: str) -> None:
        """Fetch pages with *fetch*, linked by *pager*, starting at *first*, for the client named *source*."""
        self._fetch = fetch
        self._pager = pager
        self._first = first
        self._source = source

    def __aiter__(self) -> AsyncIterator[I]:
        """Iterate the items of every page, in order."""
        return self._items()

    async def pages(self) -> AsyncIterator[P]:
        """Iterate the pages, in order."""
        request: PageRequest | None = self._pager.first(self._first)
        seen: set[str] = set()
        while request is not None:
            key = _key(request)
            if key in seen:
                raise self._loop_error(request, len(seen))
            seen.add(key)
            page, response = await self._fetch(request)
            request = self._pager.next_page(page, request, response)
            del response
            yield page

    async def _items(self) -> AsyncIterator[I]:
        """Iterate the items of every page, in order."""
        async for page in self.pages():
            for item in self._pager.page_items(page):
                yield item

    def _loop_error(self, request: PageRequest, fetched: int) -> ClientError:
        """Build the error for *request*, which repeats one of the *fetched* pages' requests."""
        url = loggable_url(request.url) if isinstance(request.url, str) else None
        where = "" if url is None else f" ({url})"
        msg = f"pagination loop: the page after page {fetched} repeats an earlier page's request{where}"
        return ClientError(msg, source=self._source, method="GET", url=url)


def _key(request: PageRequest) -> str:
    """Return a text key identifying *request*'s URL and query."""
    url = request.url if isinstance(request.url, str) else None
    query = None if request.params is None else tuple(request.params.items())
    return repr((url, query))
