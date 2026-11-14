# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""The lazy sequence of pages and items :meth:`Client.paginate <headgate.Client.paginate>` returns."""

from __future__ import annotations

import hashlib
from contextlib import aclosing
from typing import TYPE_CHECKING, Final, Literal, Protocol, final

import httpx2

from headgate._core import check_int
from headgate._failures import loggable_url, shorten
from headgate._url import redact_secrets
from headgate.errors import ClientError, PageLimitError
from headgate.pagination._pager import BodyOffset, Offset, PageNumber, WritingPager

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Sequence

    from headgate._request_body import Writer
    from headgate.pagination._pager import Pager, PageRequest

__all__ = ["REMEMBERED_PAGES", "OnLimit", "PageWalk", "Paginated", "Remembered", "check_page_budget"]

# shortcut: a cycle longer than 4096 pages goes undetected (max_pages= still ends it); raise this if a real walk ever
# loops past it, at about 100 bytes of memory per page remembered.
#: How many of the most recent pages a walk remembers to catch a loop: any loop a real server makes is far shorter.
REMEMBERED_PAGES: Final = 4096

#: The bytes a remembered key is cut to: a digest, so a page's key costs the same whatever its body's size.
_DIGEST_BYTES: Final = 16


#: The outcomes ``on_limit=`` names: raise ``PageLimitError``, or end the walk.
type OnLimit = Literal["raise", "stop"]


def check_page_budget(max_pages: int | None, on_limit: OnLimit) -> None:
    """Refuse a page budget a walk cannot keep: a count below one page or not an ``int``, an unknown outcome, a stop.

    :raises TypeError: for a *max_pages* that is not an ``int``, a ``bool`` included.
    :raises ValueError: for a *max_pages* below 1, an *on_limit* that is neither ``"raise"`` nor ``"stop"``, and
        ``on_limit="stop"`` without *max_pages*, which says what happens at a limit that does not exist.
    """
    wanted = "None or an int of at least 1"
    if max_pages is not None:
        check_int("max_pages", max_pages, wanted)
        if max_pages < 1:
            msg = f"max_pages= takes {wanted} (got {max_pages})"
            raise ValueError(msg)
    if on_limit not in {"raise", "stop"}:
        msg = f"on_limit= takes 'raise' or 'stop' (got {on_limit!r})"
        raise ValueError(msg)
    if on_limit == "stop" and max_pages is None:
        msg = "on_limit='stop' says what happens at max_pages=, so it needs one"
        raise ValueError(msg)


type Fetch[P, B] = Callable[[PageRequest[B]], Awaitable[tuple[P, httpx2.Response]]]
"""Fetches one page through the client, returning what the call's reader read and the response it read it from."""


class Paginated[I, P = object](Protocol):
    """The pages of a paginated endpoint and the items on them, fetched one page at a time as iteration reaches them.

    ``async for item in paginated`` yields every item; :meth:`pages` yields the pages themselves; :meth:`collect`
    returns the items as a list, optionally at most so many of them. Nothing is fetched until iteration starts, no
    page is held after the iteration moves past it, and each iteration starts again from the first page. Stopping
    early fetches nothing more.

    A page that leads back to a request already made (a cursor or next URL that repeats) raises ``ClientError``
    instead of looping forever; the requests made are remembered for that, as a digest per page of its URL (as written
    and as sent), query and, for a POST, the bytes its pager's ``write`` encodes the body to, or the ``repr`` of a body
    of builtins sent as JSON. An :class:`~headgate.Offset`, :class:`~headgate.PageNumber` or
    :class:`~headgate.pagination.BodyOffset` walk builds each request from the last, so a server that ignores its
    paging would send the same page forever: a page with items whose body repeats an earlier page's raises
    ``ClientError`` too. At most :data:`REMEMBERED_PAGES` of each are remembered, the oldest forgotten first. A walk
    with ``max_pages=`` fetches at most that many pages, then raises ``PageLimitError`` or, with ``on_limit="stop"``,
    stops.

    ``I`` is the item type and ``P`` the page type, which defaults to ``object``: ``Paginated[Issue]`` is a walk of
    issues, and ``Paginated[Issue, IssuePage]`` names the pages :meth:`pages` yields too.
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
    """The :class:`Paginated` :meth:`Client.paginate <headgate.Client.paginate>` builds."""

    __slots__ = ("_fetch", "_first", "_items_of", "_max_pages", "_on_limit", "_pager", "_source", "_write")

    def __init__(
        self,
        fetch: Fetch[P, B],
        pager: Pager[P, I, B],
        first: PageRequest[B],
        *,
        source: str,
        max_pages: int | None = None,
        on_limit: OnLimit = "raise",
    ) -> None:
        """Fetch pages with *fetch*, linked by *pager*, starting at *first*, for the client named *source*.

        *first* is the first page's request as the pager prepared it (its :meth:`~Pager.first`), so every iteration
        starts from it and the pager's refusals were raised when the walk was built.

        *max_pages* is the walk's page budget, or ``None`` for none, and *on_limit* what happens when it is spent with
        pages to come, both checked by :func:`check_page_budget`.
        """
        self._fetch = fetch
        self._pager = pager
        self._first = first
        self._source = source
        self._max_pages = max_pages
        self._on_limit = on_limit
        # A page is remembered by the body it sends: the bytes the writer encodes, as the walk's fetch encodes them.
        self._write = pager.write if isinstance(pager, WritingPager) else None
        # The pagers whose next request is built from the last, not read from the page: only a page's body can tell
        # that the server ignored the paging.
        self._items_of: Callable[[P], Sequence[I]] | None = (
            pager.items if isinstance(pager, Offset | PageNumber | BodyOffset) else None
        )

    def __aiter__(self) -> AsyncIterator[I]:
        """Iterate the items of every page, in order."""
        return self._items()

    async def pages(self) -> AsyncIterator[P]:
        """Iterate the pages, in order."""
        request: PageRequest[B] | None = self._first
        requests, bodies = Remembered(), Remembered()
        fetched = 0
        while request is not None:
            if fetched == self._max_pages:
                if self._on_limit == "stop":
                    return
                raise self._limit_error(request, fetched)
            if not requests.add(_key(request, self._write)):
                raise self._loop_error(request, fetched)
            page, response = await self._fetch(request)
            fetched += 1
            if request.body is None:
                # A next URL from the wire is absolute, so the page is remembered as it was sent too.
                requests.add(_sent_key(response))
            if self._items_of is not None and self._items_of(page) and not bodies.add(response.content):
                raise self._repeat_error(request, fetched, response)
            try:
                request = self._pager.next_page(page, request, response)
            except httpx2.InvalidURL as exc:
                raise self._invalid_error(request, fetched, exc) from exc
            del response
            yield page

    async def collect(self, *, max_items: int | None = None) -> list[I]:
        """Return every item of the walk, in order; with *max_items*, at most that many.

        The walk stops as soon as it has *max_items* items, so no page past the one that filled it is fetched, and
        ``max_items=0`` sends no request. A page budget (``max_pages=``) applies as it does to iteration: a walk it cuts
        short raises ``PageLimitError`` unless the items were all collected first.
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
        url = _loggable(request)
        where = "" if url is None else f" ({url})"
        msg = (
            f"pagination reached its limit of {fetched} pages with more to come{where}; the result is incomplete. "
            "Raise max_pages=, or set on_limit='stop' to end the walk there"
        )
        return PageLimitError(msg, pages=fetched, source=self._source, method=_method(request), url=url)

    def _repeat_error(self, request: PageRequest[B], fetched: int, response: httpx2.Response) -> ClientError:
        """Build the error for *request*, page *fetched*, whose body (in *response*) repeats an earlier page's."""
        url = loggable_url(str(response.request.url))
        msg = f"pagination loop: page {fetched} repeats an earlier page's body, so the server is not paging the walk"
        msg += f" ({url})"
        return ClientError(msg, source=self._source, method=_method(request), url=url)

    def _invalid_error(self, request: PageRequest[B], fetched: int, exc: httpx2.InvalidURL) -> ClientError:
        """Build the error for the next-page URL page *fetched* (fetched by *request*) named, which httpx2 refused."""
        msg = f"invalid next-page URL after page {fetched}: {shorten(redact_secrets(str(exc)))}"
        return ClientError(msg, source=self._source, method=_method(request), url=_loggable(request))

    def _loop_error(self, request: PageRequest[B], fetched: int) -> ClientError:
        """Build the error for *request*, which repeats one of the *fetched* pages' requests."""
        url = _loggable(request)
        where = "" if url is None else f" ({url})"
        msg = f"pagination loop: the page after page {fetched} repeats an earlier page's request{where}"
        return ClientError(msg, source=self._source, method=_method(request), url=url)


def _loggable[B](request: PageRequest[B]) -> str | None:
    """Return *request*'s URL as safe to log, or ``None`` for a template, which has no single URL."""
    return loggable_url(str(request.url)) if isinstance(request.url, str | httpx2.URL) else None


@final
class Remembered:
    """The digests of the last :data:`REMEMBERED_PAGES` keys added, oldest forgotten first."""

    __slots__ = ("_keys",)

    def __init__(self) -> None:
        """Remember nothing yet."""
        self._keys: dict[bytes, None] = {}

    def __len__(self) -> int:
        """Return how many keys are remembered."""
        return len(self._keys)

    def add(self, key: bytes) -> bool:
        """Remember *key*'s digest; return ``False`` when it was remembered already."""
        digest = _digest(key)
        if digest in self._keys:
            return False
        self._keys[digest] = None
        if len(self._keys) > REMEMBERED_PAGES:
            del self._keys[next(iter(self._keys))]
        return True


def _digest(data: bytes) -> bytes:
    """Return a fixed-size digest of *data*."""
    return hashlib.blake2b(data, digest_size=_DIGEST_BYTES).digest()


def _key[B](request: PageRequest[B], write: Writer[B] | None) -> bytes:
    """Return a key identifying *request*'s URL (a string or a template, by its parts), query and body.

    A body is keyed by the bytes *write* encodes it to, as it is sent, since a body type's ``repr`` may be its address,
    which a later body can reuse. Without *write*, a body is builtins sent as JSON, whose ``repr`` is their value.
    """
    query = None if request.params is None else tuple(request.params.items())
    body = request.body if request.body is None or write is None else write(request.body).content
    return repr((request.url, query, body)).encode()


def _sent_key(response: httpx2.Response) -> bytes:
    """Return the key of a GET for the URL *response*'s first hop was sent to, as a next URL from the wire is keyed."""
    sent = (response.history[0] if response.history else response).request.url
    return repr((sent, None, None)).encode()


def _method[B](request: PageRequest[B]) -> Literal["GET", "POST"]:
    """Return the method *request* is sent with: a POST when it has a body."""
    return "GET" if request.body is None else "POST"
