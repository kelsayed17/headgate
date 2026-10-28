# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""How a paginated endpoint links its pages: the :class:`Pager` protocol and its strategies.

A pager is a frozen value that knows two things about a page ``P`` it never fetches itself: the items ``I`` on it, and
the request for the page after it. The client does the fetching (:meth:`AsyncBaseClient.paginate
<headgate.AsyncBaseClient.paginate>`), so every page goes through the whole stack.

* :class:`Cursor`: the page names an opaque cursor, sent back as a query parameter on the same URL.
* :class:`Offset`: the next page starts where this one ended, as an offset query parameter.
* :class:`LinkHeader`: the response's ``Link`` header names the next page (RFC 8288 ``rel="next"``).
* :class:`NextUrl`: the page's body names the next page's URL.
* :class:`BodyCursor`: like :class:`Cursor`, but the cursor is put into the POSTed request body.
* :class:`BodyOffset`: like :class:`Offset`, but the offset is put into the POSTed request body.

A next-page URL from the wire is resolved against the URL that answered and sent as an absolute URL, which the client
treats like any other: credentials go only to the origins the client trusts.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import TYPE_CHECKING, Protocol, override

from headgate.pagination._link import next_link

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping, Sequence
    from string.templatelib import Template

    import httpx2

    from headgate._request_body import Writer

__all__ = [
    "BodyCursor",
    "BodyOffset",
    "Cursor",
    "LinkHeader",
    "NextUrl",
    "Offset",
    "PageRequest",
    "Pager",
    "WritingPager",
]


@dataclass(frozen=True, slots=True)
class PageRequest[B = None]:
    """The request for one page, as a :class:`Pager` reads and returns it: its URL (as a verb takes it), its query.

    ``paginate`` builds the first from its URL, ``params=`` and body; a pager derives each next one from it.

    A request with no ``body`` is a GET, and one with a ``body`` a POST. The body stays the caller's own value, of type
    ``B``, so that a pager can rewrite it for the next page; how it is sent is the walk's, not the request's (see
    :class:`WritingPager`).
    """

    url: str | Template
    params: Mapping[str, object] | None = None
    body: B | None = None

    def __post_init__(self) -> None:
        """Freeze the query."""
        if self.params is not None:
            object.__setattr__(self, "params", MappingProxyType(dict(self.params)))

    def with_param(self, name: str, value: object) -> PageRequest[B]:
        """Return this request with the query parameter *name* set to *value*."""
        return replace(self, params={**(self.params or {}), name: value})

    def with_body(self, body: B) -> PageRequest[B]:
        """Return this request with *body* as its body."""
        return replace(self, body=body)


class Pager[P, I, B = None](Protocol):
    """How pages of type ``P`` holding items of type ``I``, fetched with request bodies of type ``B``, link up.

    ``B`` is the type of the walk's body, ``None`` for a pager that only walks GETs. A pager typed with a body may walk
    GETs too, when ``paginate`` is given no body (``body=None``): the first request's ``body`` is then ``None``, and a
    later one has a body only if the pager gives it one.
    """

    __slots__ = ()

    def first(self, request: PageRequest[B]) -> PageRequest[B]:
        """Return the request for the first page, given the one the caller wrote.

        It runs once, when :meth:`~headgate.AsyncBaseClient.paginate` builds the walk, and every iteration starts from
        what it returned, so it is a function of *request* alone: nothing that should change between iterations, such
        as the time, belongs here. Its refusals raise from ``paginate``.
        """
        ...

    def page_items(self, page: P) -> Iterable[I]:
        """Return the items on *page*."""
        ...

    def next_page(self, page: P, request: PageRequest[B], response: httpx2.Response) -> PageRequest[B] | None:
        """Return the request for the page after *page* (fetched by *request*, answered by *response*), or ``None``."""
        ...


class WritingPager[P, I, B](Pager[P, I, B], ABC):
    """A :class:`Pager` that writes the walk's request bodies: ``write`` encodes a ``body=`` for every page.

    A walk given ``body=`` needs one, since the body is the caller's own value, and a walk given ``json=`` needs one
    whose ``write`` is ``None``, since builtins are sent as JSON. :class:`Cursor`, :class:`Offset`,
    :class:`BodyCursor` and :class:`BodyOffset` are writing pagers; a pager of GETs is not one.

    A pager is one by subclassing this class, not by its shape: an attribute that only shares the name ``write`` is
    never taken for a writer. Every member is abstract, so a subclass that leaves one out cannot be built.
    """

    __slots__ = ()

    @property
    @abstractmethod
    def write(self) -> Writer[B] | None:
        """The writer (a codec's ``encoder``) that encodes each page's body, or ``None`` for one sent as JSON."""

    @override
    @abstractmethod
    def first(self, request: PageRequest[B]) -> PageRequest[B]:
        """Return the request for the first page, given the one the caller wrote (see :meth:`Pager.first`)."""

    @override
    @abstractmethod
    def page_items(self, page: P) -> Iterable[I]:
        """Return the items on *page*."""

    @override
    @abstractmethod
    def next_page(self, page: P, request: PageRequest[B], response: httpx2.Response) -> PageRequest[B] | None:
        """Return the request for the page after *page* (fetched by *request*, answered by *response*), or ``None``."""


def _check_param(param: str) -> None:
    """Refuse an empty query parameter name."""
    if not param:
        msg = "param must name a query parameter"
        raise ValueError(msg)


def _absolute(response: httpx2.Response, target: str) -> PageRequest:
    """Return the request for *target*, resolved against the URL that sent *response*, as an absolute URL."""
    return PageRequest(str(response.url.join(target)))


def _check_get[B](request: PageRequest[B], pager: str) -> None:
    """Refuse a walk with a request body for *pager*, whose next page is a URL fetched with a GET."""
    if request.body is not None:
        msg = f"{pager} follows next-page URLs with GETs, so its walk takes no request body"
        raise ValueError(msg)


def _posted[B](request: PageRequest[B], pager: str) -> B:
    """Return *request*'s body, refusing a request with none: *pager* rewrites the body, so its walk is POSTs."""
    if request.body is None:
        msg = f"{pager} rewrites the request body, so its walk needs one: pass body= or json="
        raise ValueError(msg)
    return request.body


@dataclass(frozen=True, slots=True, kw_only=True)
class Cursor[P, I, B = None](WritingPager[P, I, B]):
    """Pages linked by an opaque cursor: ``next`` reads it from a page, and it is sent back as the query ``param``.

    The last page is the one whose ``next`` returns ``None`` or ``""``, or one with no items: a cursor that advances
    while returning nothing makes no progress. Set ``stop_on_empty=False`` for an endpoint that sends empty pages
    mid-walk. ``B`` is the type of the request body for a walk of POSTs (``Cursor[Page, Item, Query]``), and
    ``None`` for a walk of GETs. ``write`` encodes a ``body=`` on each page (a codec's ``encoder``); a walk of POSTs
    without one sends ``json=``, and a walk of GETs leaves it unused.
    """

    items: Callable[[P], Sequence[I]]
    next: Callable[[P], str | None]
    param: str
    write: Writer[B] | None = None
    stop_on_empty: bool = True

    def __post_init__(self) -> None:
        """Refuse an empty parameter name."""
        _check_param(self.param)

    @override
    def first(self, request: PageRequest[B]) -> PageRequest[B]:
        """Return *request* unchanged: the first page is fetched without a cursor."""
        return request

    @override
    def page_items(self, page: P) -> Sequence[I]:
        """Return the items on *page*."""
        return self.items(page)

    @override
    def next_page(self, page: P, request: PageRequest[B], response: httpx2.Response) -> PageRequest[B] | None:
        """Return *request* carrying *page*'s cursor, or ``None`` when it has none or (by default) no items."""
        del response
        if self.stop_on_empty and not self.items(page):
            return None
        cursor = self.next(page)
        return request.with_param(self.param, cursor) if bool(cursor) else None


@dataclass(frozen=True, slots=True, kw_only=True)
class Offset[P, I, B = None](WritingPager[P, I, B]):
    """Pages of at most ``page_size`` items, addressed by the offset of their first item, sent as the query ``param``.

    The offset starts at ``start`` and advances by the number of items each page held. A page holding fewer than
    ``page_size`` items (or none) is the last. The page size itself is the caller's to send, in ``params=`` under
    whatever name the endpoint uses, since that name varies (``limit``, ``per_page``, ``page_size``). ``B`` is the
    type of the request body for a walk of POSTs, and ``write`` its writer, as on :class:`Cursor`.
    """

    items: Callable[[P], Sequence[I]]
    page_size: int
    param: str = "offset"
    start: int = 0
    write: Writer[B] | None = None

    def __post_init__(self) -> None:
        """Refuse a page size below one, a negative start and an empty parameter name."""
        if self.page_size < 1:
            msg = f"page_size must be >= 1 (got {self.page_size})"
            raise ValueError(msg)
        if self.start < 0:
            msg = f"start must be >= 0 (got {self.start})"
            raise ValueError(msg)
        _check_param(self.param)

    @override
    def first(self, request: PageRequest[B]) -> PageRequest[B]:
        """Return *request* at offset ``start``."""
        return request.with_param(self.param, self.start)

    @override
    def page_items(self, page: P) -> Sequence[I]:
        """Return the items on *page*."""
        return self.items(page)

    @override
    def next_page(self, page: P, request: PageRequest[B], response: httpx2.Response) -> PageRequest[B] | None:
        """Return *request* at the offset after *page*'s items, or ``None`` when *page* was short."""
        del response
        count = len(self.items(page))
        if count < self.page_size:
            return None
        offset = (request.params or {}).get(self.param)
        current = offset if isinstance(offset, int) else self.start
        return request.with_param(self.param, current + count)


@dataclass(frozen=True, slots=True, kw_only=True)
class LinkHeader[P, I]:
    """Pages linked by the response's ``Link`` header: the target of its ``rel="next"`` link is the next page.

    The last page is the one with no ``rel="next"`` link, or one with no items (``stop_on_empty=False`` follows the
    link past an empty page).
    """

    items: Callable[[P], Sequence[I]]
    stop_on_empty: bool = True

    def first(self, request: PageRequest) -> PageRequest:
        """Return *request* unchanged, refusing one with a body: a ``Link`` target is fetched with a GET."""
        _check_get(request, "LinkHeader")
        return request

    def page_items(self, page: P) -> Sequence[I]:
        """Return the items on *page*."""
        return self.items(page)

    def next_page(self, page: P, request: PageRequest, response: httpx2.Response) -> PageRequest | None:
        """Return the request for *response*'s ``rel="next"`` link, or ``None`` when it has none or *page* is empty."""
        del request
        if self.stop_on_empty and not self.items(page):
            return None
        target = next_link(response.headers)
        return None if target is None else _absolute(response, target)


@dataclass(frozen=True, slots=True, kw_only=True)
class NextUrl[P, I]:
    """Pages linked by a URL in the body: ``next`` reads the next page's URL (absolute or relative) from a page.

    The last page is the one whose ``next`` returns ``None`` or ``""``, or one with no items (``stop_on_empty=False``
    follows the URL past an empty page).
    """

    items: Callable[[P], Sequence[I]]
    next: Callable[[P], str | None]
    stop_on_empty: bool = True

    def first(self, request: PageRequest) -> PageRequest:
        """Return *request* unchanged, refusing one with a body: a next URL is fetched with a GET."""
        _check_get(request, "NextUrl")
        return request

    def page_items(self, page: P) -> Sequence[I]:
        """Return the items on *page*."""
        return self.items(page)

    def next_page(self, page: P, request: PageRequest, response: httpx2.Response) -> PageRequest | None:
        """Return the request for *page*'s next URL, or ``None`` when it names none or (by default) has no items."""
        del request
        if self.stop_on_empty and not self.items(page):
            return None
        target = self.next(page)
        return _absolute(response, target) if bool(target) else None


@dataclass(frozen=True, slots=True, kw_only=True)
class BodyCursor[P, I, B](WritingPager[P, I, B]):
    """Pages linked by an opaque cursor carried in the POSTed body: ``put`` writes it into the next page's body.

    For an endpoint whose query is a request body (a search API, a JSON-RPC call): the walk POSTs the body the caller
    passed ``paginate``, and each next page POSTs ``put(body, cursor)``, the same body carrying the cursor ``next``
    read from the page. ``put`` returns a new body, leaving the one it was given as it was. ``write`` encodes each
    page's body (a codec's ``encoder``); without it the body is sent as JSON, so it is builtins. The last page is the
    one whose ``next`` returns ``None`` or ``""``, or one with no items (``stop_on_empty=False`` follows the cursor
    past an empty page).
    """

    items: Callable[[P], Sequence[I]]
    next: Callable[[P], str | None]
    put: Callable[[B, str], B]
    write: Writer[B] | None = None
    stop_on_empty: bool = True

    @override
    def first(self, request: PageRequest[B]) -> PageRequest[B]:
        """Return *request* unchanged, refusing one with no body: the first page is POSTed without a cursor."""
        _posted(request, "BodyCursor")
        return request

    @override
    def page_items(self, page: P) -> Sequence[I]:
        """Return the items on *page*."""
        return self.items(page)

    @override
    def next_page(self, page: P, request: PageRequest[B], response: httpx2.Response) -> PageRequest[B] | None:
        """Return *request* with *page*'s cursor in its body, or ``None`` when it has none or (by default) no items."""
        del response
        if self.stop_on_empty and not self.items(page):
            return None
        cursor = self.next(page)
        if cursor is None or not cursor:
            return None
        return request.with_body(self.put(_posted(request, "BodyCursor"), cursor))


@dataclass(frozen=True, slots=True, kw_only=True)
class BodyOffset[P, I, B](WritingPager[P, I, B]):
    """Pages of at most ``page_size`` items, addressed by the offset of their first item, carried in the POSTed body.

    ``offset`` reads the offset from a body and ``put`` returns a new body at another one. The walk starts at the
    offset the first request's body carries, and advances by the number of items each page held. A page holding fewer
    than ``page_size`` items (or none) is the last. The page size itself is the caller's to put in the body, as it is
    the caller's to put in :class:`Offset`'s query. ``write`` encodes each page's body, as for :class:`BodyCursor`.
    """

    items: Callable[[P], Sequence[I]]
    page_size: int
    offset: Callable[[B], int]
    put: Callable[[B, int], B]
    write: Writer[B] | None = None

    def __post_init__(self) -> None:
        """Refuse a page size below one."""
        if self.page_size < 1:
            msg = f"page_size must be >= 1 (got {self.page_size})"
            raise ValueError(msg)

    @override
    def first(self, request: PageRequest[B]) -> PageRequest[B]:
        """Return *request* unchanged, refusing one with no body or a negative offset."""
        start = self.offset(_posted(request, "BodyOffset"))
        if start < 0:
            msg = f"the first request body's offset must be >= 0 (got {start})"
            raise ValueError(msg)
        return request

    @override
    def page_items(self, page: P) -> Sequence[I]:
        """Return the items on *page*."""
        return self.items(page)

    @override
    def next_page(self, page: P, request: PageRequest[B], response: httpx2.Response) -> PageRequest[B] | None:
        """Return *request* with its body at the offset after *page*'s items, or ``None`` when *page* was short."""
        del response
        count = len(self.items(page))
        if count < self.page_size:
            return None
        body = _posted(request, "BodyOffset")
        return request.with_body(self.put(body, self.offset(body) + count))
