# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""The lazy sequence of pages and items :meth:`Client.paginate <headgate.Client.paginate>` returns."""

import hashlib
import itertools
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Iterable, Iterator
from contextlib import aclosing
from dataclasses import replace
from typing import Final, Literal, cast, final

import httpx2

from headgate._core import check_callable, check_int, check_kind, sealed
from headgate._errors import ClientError
from headgate._failures import loggable_url, shorten
from headgate._request_body import EncodedBody
from headgate._url import redact_secrets
from headgate.errors import ForbiddenDestinationError, PageLimitError
from headgate.pagination._pager import ITEMS, Pager, PageRequest, WritingPager, items_hook, returned, written

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

    :raises TypeError: for a *max_pages* that is not an ``int``, a ``bool`` included, and an *on_limit* that is not a
        ``str``.
    :raises ValueError: for a *max_pages* below 1, an *on_limit* that is neither ``"raise"`` nor ``"stop"``, and
        ``on_limit="stop"`` without *max_pages*, which says what happens at a limit that does not exist.
    """
    wanted = "None or an int of at least 1"
    if max_pages is not None:
        check_int("max_pages", max_pages, wanted)
        if max_pages < 1:
            msg = f"max_pages= takes {wanted} (got {max_pages})"
            raise ValueError(msg)
    check_kind("on_limit", on_limit, str, "'raise' or 'stop'")
    if on_limit not in {"raise", "stop"}:
        msg = f"on_limit= takes 'raise' or 'stop' (got {on_limit!r})"
        raise ValueError(msg)
    if on_limit == "stop" and max_pages is None:
        msg = "on_limit='stop' says what happens at max_pages=, so it needs one"
        raise ValueError(msg)


type Fetch[P, B] = Callable[[PageRequest[B], EncodedBody | None], Awaitable[tuple[P, httpx2.Response]]]
"""Fetches one page through the client, returning what the call's reader read and the response it read it from.

It is given the page's body as the pager's writer encoded it, once, for the walk's loop guard and the send alike, or
``None`` for a GET or a body sent as JSON.
"""


@final
@sealed("build one of its pages, Paginated(pages, items_of)")
class Paginated[I, P = object]:
    """The pages of a paginated endpoint and the items on them, fetched one page at a time as iteration reaches them.

    ``async for item in paginated`` yields every item; :meth:`pages` yields the pages themselves; :meth:`collect`
    returns the items as a list, optionally at most so many of them. Nothing is fetched until iteration starts, no
    page is held after the iteration moves past it, and each iteration starts again from the first page. Stopping
    early fetches nothing more.

    A page that leads back to a request already made (a cursor or next URL that repeats) raises ``ClientError``
    instead of looping forever; the requests made are remembered for that, as a digest per page of its URL (as written
    and as sent), params and, for a POST, the bytes its pager's ``write`` encodes the body to, or the ``repr`` of a body
    of builtins sent as JSON. A pager that counts its pages (``counts_pages``: an offset, a page number) sends requests
    that never repeat, so a server that ignores its paging would send the same page forever: under such a pager, a page
    with items whose body repeats an earlier page's raises ``ClientError`` too. At most 4096 of each are remembered,
    the oldest forgotten first. A walk with ``max_pages=`` fetches at most that many pages, then
    raises ``PageLimitError`` or, with ``on_limit="stop"``, stops.

    ``I`` is the item type and ``P`` the page type, which defaults to ``object``: ``Paginated[Issue]`` is a walk of
    issues, and ``Paginated[Issue, IssuePage]`` names the pages :meth:`pages` yields too.

    :meth:`Client.paginate <headgate.Client.paginate>` builds one from its walk. A test double of an SDK builds one
    from its own pages: ``Paginated(pages, items_of)``, where *pages* is a function of no arguments that starts a fresh
    iteration of the pages each time it is called, and *items_of* returns the items on one page, so the items always
    come from the pages.
    """

    __slots__ = ("_items_of", "_pages")

    def __init__(self, pages: Callable[[], AsyncIterator[P]], items_of: Callable[[P], Iterable[I]], /) -> None:
        """Start each iteration with *pages*, and read each page's items with *items_of*.

        :raises TypeError: for a *pages* or an *items_of* that cannot be called.
        """
        check_callable(
            "Paginated(pages)", pages, "a function that starts the pages afresh, such as an async generator function"
        )
        check_callable("Paginated(items_of)", items_of, "a function returning a page's items")
        self._pages: Final = pages
        # Read only with pages this walk yielded, so it is held as taking any object: P stays covariant, and a
        # Paginated[Issue, IssuePage] is a Paginated[Issue].
        self._items_of: Final = cast("Callable[[object], Iterable[I]]", items_of)

    def __aiter__(self) -> AsyncIterator[I]:
        """Iterate the items of every page, in order."""
        return self._items()

    def pages(self) -> AsyncIterator[P]:
        """Iterate the pages, in order."""
        return self._pages()

    async def collect(self, *, max_items: int | None = None) -> list[I]:
        """Return every item of the walk, in order; with *max_items*, at most that many.

        The walk stops as soon as it has *max_items* items, so no page past the one that filled it is fetched, and
        ``max_items=0`` sends no request. A page budget (``max_pages=``) applies as it does to iteration: a walk it cuts
        short raises ``PageLimitError`` unless the items were all collected first.

        :raises TypeError: for a *max_items* that is not an ``int``, a ``bool`` included.
        :raises ValueError: for a *max_items* below 0.
        """
        if max_items is not None:
            check_int("max_items", max_items, "None or an int of 0 or more")
            if max_items < 0:
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
        async for page in self._pages():
            for item in self._items_of(page):
                yield item


@final
class PageWalk[I, P, B]:
    """The pages of one walk, and the items on them: what the :class:`Paginated` ``paginate`` returns iterates."""

    __slots__ = (
        "_counts",
        "_fetch",
        "_first",
        "_given_items",
        "_held",
        "_max_pages",
        "_on_limit",
        "_pager",
        "_source",
        "_write",
    )

    def __init__(
        self,
        fetch: Fetch[P, B],
        pager: Pager[I, P, B],
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
        self._counts = getattr(pager, "counts_pages", False) is True
        self._given_items = items_hook(pager)
        # The items of a page the loop guard looked into, by the page's id, until the walk moves past the page.
        self._held: dict[int, Iterable[I]] = {}

    def _next_page(
        self, page: P, items: Iterable[I], request: PageRequest[B], response: httpx2.Response
    ) -> PageRequest[B] | None:
        """Return the request after *page*, handing a built-in strategy the *items* already read.

        Any other pager reads what it needs of the page itself.
        """
        if self._given_items is None:
            return self._pager.next_page(page, request, response)
        return cast("PageRequest[B] | None", self._given_items(page, items, request, response))

    def items_of(self, page: P) -> Iterable[I]:
        """Return the items on *page*: the ones the loop guard already read, else the pager's, read once either way."""
        held = self._held.pop(id(page), None)
        return self._pager.page_items(page) if held is None else held

    def _read_items(self, page: P) -> tuple[Iterable[I], bool]:
        """Read *page*'s items, once: return them, whole, and whether there is one.

        An iterator gives up its first item to be seen, so it is put back in front of the rest.
        """
        items = self._pager.page_items(page)
        # A built-in strategy's page_items has already refused an items= whose return it cannot count, naming items=.
        returned(f"{type(self._pager).__name__}.page_items()", items, Iterable, ITEMS, optional=False)
        if not isinstance(items, Iterator):
            return items, next(iter(items), _NO_ITEM) is not _NO_ITEM
        first = next(items, _NO_ITEM)
        if first is _NO_ITEM:
            return items, False
        return itertools.chain((cast("I", first),), items), True

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
            # The writer runs once a page: its bytes are both what the loop guard remembers and what is sent.
            encoded = None if request.payload is None or self._write is None else written(self._write, request.payload)
            if not requests.add(_key(request, encoded)):
                raise self._loop_error(request, fetched)
            page, response = await self._fetch(request, encoded)
            fetched += 1
            if request.payload is None:
                # A next URL from the wire is absolute, so the page is remembered as it was sent too.
                requests.add(_sent_key(response))
            try:
                # A page's items are read once, here, and held for the item walk; a built-in strategy is given them
                # for its next request too, so its items= hook runs once per page.
                items, has_items = self._read_items(page)
                self._held[id(page)] = items
                # A server that ignores counted paging sends one page forever under requests that never repeat: only
                # a page's body tells. An empty page proves nothing, so it is not remembered.
                if self._counts and has_items and not bodies.add(response.content):
                    raise self._repeat_error(request, fetched, response)
                try:
                    request = self._next_page(page, items, request, response)
                    if request is not None:
                        request = _resolved(request, response)
                except httpx2.InvalidURL as exc:
                    raise self._invalid_error(request, fetched, exc) from exc
                del response
                yield page
            finally:
                # Held until the walk moves past the page, as no page is held after that.
                self._held.pop(id(page), None)

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


#: What :meth:`PageWalk._held_items` reads from a page with no items: no item a page can hold.
_NO_ITEM: Final = object()


def _resolved[B](request: PageRequest[B], response: httpx2.Response) -> PageRequest[B]:
    """Return *request* with a wire URL resolved against the URL that answered *response*, the page before it.

    A pager forwards a URL from the wire as an ``httpx2.URL``, absolute or relative, which is resolved against the
    answering URL; or as a ``str`` read from a body, which is checked when it is absolute. A relative ``str`` and a
    template are the caller's own, and the client joins them onto ``base_url`` as it does any call's.

    :raises ForbiddenDestinationError: for a plain ``http`` URL after an ``https`` page, which would send the next
        request (its credentials and cursor) in cleartext.
    """
    if isinstance(request.url, httpx2.URL):
        target = request.url
    elif isinstance(request.url, str) and (parsed := httpx2.URL(request.url)).is_absolute_url:
        target = parsed
    else:
        return request
    url = response.url.join(target)
    if response.url.scheme == "https" and url.scheme == "http":
        msg = f"refused to follow a next page from https to {loggable_url(str(url))}"
        raise ForbiddenDestinationError(msg)
    return replace(request, url=url)


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


def _key[B](request: PageRequest[B], encoded: EncodedBody | None) -> bytes:
    """Return a key identifying *request*'s URL (a string or a template, by its parts), query and body.

    A body a writer encoded is keyed by those bytes, *encoded*, as it is sent, since a body type's ``repr`` may be its
    address, which a later body can reuse. Without one, a body is builtins sent as JSON, whose ``repr`` is their value.
    """
    query = None if request.params is None else tuple(request.params.items())
    body = request.payload if encoded is None else encoded.content
    return repr((request.url, query, body)).encode()


def _sent_key(response: httpx2.Response) -> bytes:
    """Return the key of a GET for the URL *response*'s first hop was sent to, as a next URL from the wire is keyed."""
    sent = (response.history[0] if response.history else response).request.url
    return repr((sent, None, None)).encode()


def _method[B](request: PageRequest[B]) -> Literal["GET", "POST"]:
    """Return the method *request* is sent with: a POST when it has a body."""
    return "GET" if request.payload is None else "POST"
