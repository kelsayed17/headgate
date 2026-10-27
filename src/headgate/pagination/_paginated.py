# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""The lazy sequence of pages and items :meth:`AsyncBaseClient.paginate <headgate.AsyncBaseClient.paginate>` returns."""

from __future__ import annotations

from contextlib import aclosing
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol, final

from headgate._failures import loggable_url
from headgate.errors import ClientError, PageLimitError

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable

    import httpx2

    from headgate.pagination._pager import Pager, PageRequest

__all__ = ["PageLimit", "PageWalk", "Paginated"]


@dataclass(frozen=True, slots=True)
class PageLimit:
    """A page budget for :meth:`AsyncBaseClient.paginate <headgate.AsyncBaseClient.paginate>`: at most ``max_pages``.

    When the budget is spent with more pages to come, ``on_limit`` says what happens: ``"raise"`` (the default) raises
    :class:`~headgate.errors.PageLimitError`, since the result is incomplete; ``"stop"`` ends the walk there, for a
    caller that wants only the first pages. A walk that ends within the budget is unaffected.
    """

    max_pages: int
    on_limit: Literal["raise", "stop"] = "raise"

    def __post_init__(self) -> None:
        """Refuse a budget below one page, and an ``on_limit`` that is neither ``"raise"`` nor ``"stop"``."""
        if isinstance(self.max_pages, bool) or self.max_pages < 1:
            msg = f"max_pages must be >= 1 (got {self.max_pages!r})"
            raise ValueError(msg)
        if self.on_limit not in {"raise", "stop"}:
            msg = f"on_limit must be 'raise' or 'stop' (got {self.on_limit!r})"
            raise ValueError(msg)


type Fetch[P, B] = Callable[[PageRequest[B]], Awaitable[tuple[P, httpx2.Response]]]
"""Fetches one page through the client, returning what the call's reader read and the response it read it from."""


class Paginated[P, I](Protocol):
    """The pages of a paginated endpoint and the items on them, fetched one page at a time as iteration reaches them.

    ``async for item in paginated`` yields every item; :meth:`pages` yields the pages themselves; :meth:`collect`
    returns the items as a list, optionally at most so many of them. Nothing is fetched until iteration starts, no
    page is held after the iteration moves past it, and each iteration starts again from the first page. Stopping
    early fetches nothing more.

    A page that leads back to a request already made (a cursor or next URL that repeats) raises ``ClientError``
    instead of looping forever; the requests made are remembered for that, one key per page (its URL, query and, for a
    POST, the ``repr`` of its body). A walk with a
    :class:`PageLimit` fetches at most its ``max_pages``, then raises ``PageLimitError`` or stops, as it says.
    """

    def __aiter__(self) -> AsyncIterator[I]:
        """Iterate the items of every page, in order."""
        ...

    def pages(self) -> AsyncIterator[P]:
        """Iterate the pages, in order."""
        ...

    async def collect(self, *, max_items: int | None = None) -> list[I]:
        """Return every item of the walk, in order; with *max_items*, at most that many."""
        ...


@final
class PageWalk[P, I, B]:
    """The :class:`Paginated` :meth:`AsyncBaseClient.paginate <headgate.AsyncBaseClient.paginate>` builds."""

    __slots__ = ("_fetch", "_first", "_limit", "_pager", "_source")

    def __init__(
        self,
        fetch: Fetch[P, B],
        pager: Pager[P, I, B],
        first: PageRequest[B],
        *,
        source: str,
        limit: PageLimit | None = None,
    ) -> None:
        """Fetch pages with *fetch*, linked by *pager*, starting at *first*, for the client named *source*.

        *limit* is the walk's page budget, or ``None`` for none.
        """
        self._fetch = fetch
        self._pager = pager
        self._first = first
        self._source = source
        self._limit = limit

    def __aiter__(self) -> AsyncIterator[I]:
        """Iterate the items of every page, in order."""
        return self._items()

    async def pages(self) -> AsyncIterator[P]:
        """Iterate the pages, in order."""
        request: PageRequest[B] | None = self._pager.first(self._first)
        seen: set[str] = set()
        while request is not None:
            if self._limit is not None and len(seen) == self._limit.max_pages:
                if self._limit.on_limit == "stop":
                    return
                raise self._limit_error(request, len(seen))
            key = _key(request)
            if key in seen:
                raise self._loop_error(request, len(seen))
            seen.add(key)
            page, response = await self._fetch(request)
            request = self._pager.next_page(page, request, response)
            del response
            yield page

    async def collect(self, *, max_items: int | None = None) -> list[I]:
        """Return every item of the walk, in order; with *max_items*, at most that many.

        The walk stops as soon as it has *max_items* items, so no page past the one that filled it is fetched, and
        ``max_items=0`` sends no request. A :class:`PageLimit` applies as it does to iteration: a walk it cuts short
        raises ``PageLimitError`` unless the items were all collected first.
        """
        if max_items is not None and (isinstance(max_items, bool) or max_items < 0):
            msg = f"max_items must be None or >= 0 (got {max_items!r})"
            raise ValueError(msg)
        collected: list[I] = []
        if max_items == 0:
            return collected
        async with aclosing(self._items()) as items:
            async for item in items:
                collected.append(item)
                if len(collected) == max_items:
                    break
        return collected

    async def _items(self) -> AsyncGenerator[I]:
        """Iterate the items of every page, in order."""
        async for page in self.pages():
            for item in self._pager.page_items(page):
                yield item

    def _limit_error(self, request: PageRequest[B], fetched: int) -> PageLimitError:
        """Build the error for *request*, the page after the *fetched* pages the budget allowed."""
        url = loggable_url(request.url) if isinstance(request.url, str) else None
        where = "" if url is None else f" ({url})"
        msg = (
            f"pagination reached its limit of {fetched} pages with more to come{where}; the result is incomplete. "
            "Raise PageLimit.max_pages, or set on_limit='stop' to end the walk there"
        )
        return PageLimitError(msg, pages=fetched, source=self._source, method=_method(request), url=url)

    def _loop_error(self, request: PageRequest[B], fetched: int) -> ClientError:
        """Build the error for *request*, which repeats one of the *fetched* pages' requests."""
        url = loggable_url(request.url) if isinstance(request.url, str) else None
        where = "" if url is None else f" ({url})"
        msg = f"pagination loop: the page after page {fetched} repeats an earlier page's request{where}"
        return ClientError(msg, source=self._source, method=_method(request), url=url)


def _key[B](request: PageRequest[B]) -> str:
    """Return a text key identifying *request*'s URL (a string or a template, by its parts), query and body."""
    query = None if request.params is None else tuple(request.params.items())
    return repr((request.url, query, request.body))


def _method[B](request: PageRequest[B]) -> Literal["GET", "POST"]:
    """Return the method *request* is sent with: a POST when it has a body."""
    return "GET" if request.body is None else "POST"
