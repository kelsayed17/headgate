# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""How a paginated endpoint links its pages: the :class:`Pager` protocol and its four strategies.

A pager is a frozen value that knows two things about a page ``P`` it never fetches itself: the items ``I`` on it, and
the request for the page after it. The client does the fetching (:meth:`AsyncBaseClient.paginate
<headgate.AsyncBaseClient.paginate>`), so every page goes through the whole stack.

* :class:`Cursor`: the page names an opaque cursor, sent back as a query parameter on the same URL.
* :class:`Offset`: the next page starts where this one ended, as an offset query parameter.
* :class:`LinkHeader`: the response's ``Link`` header names the next page (RFC 8288 ``rel="next"``).
* :class:`NextUrl`: the page's body names the next page's URL.

A next-page URL from the wire is resolved against the URL that answered and sent as an absolute URL, which the client
treats like any other: credentials go only to the origins the client trusts.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Protocol

from headgate.pagination._link import next_link

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping, Sequence
    from string.templatelib import Template

    import httpx2

__all__ = ["Cursor", "LinkHeader", "NextUrl", "Offset", "PageRequest", "Pager"]


@dataclass(frozen=True, slots=True)
class PageRequest:
    """The GET for one page: its URL (as a verb takes it) and its query, which replaces the call's ``params=``."""

    url: str | Template
    params: Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        """Freeze the query."""
        if self.params is not None:
            object.__setattr__(self, "params", MappingProxyType(dict(self.params)))

    def with_param(self, name: str, value: object) -> PageRequest:
        """Return this request with the query parameter *name* set to *value*."""
        return PageRequest(self.url, {**(self.params or {}), name: value})


class Pager[P, I](Protocol):
    """How pages of type ``P`` holding items of type ``I`` link to each other."""

    def first(self, request: PageRequest) -> PageRequest:
        """Return the request for the first page, given the one the caller wrote."""
        ...

    def page_items(self, page: P) -> Iterable[I]:
        """Return the items on *page*."""
        ...

    def next_page(self, page: P, request: PageRequest, response: httpx2.Response) -> PageRequest | None:
        """Return the request for the page after *page* (fetched by *request*, answered by *response*), or ``None``."""
        ...


def _check_param(param: str) -> None:
    """Refuse an empty query parameter name."""
    if not param:
        msg = "param must name a query parameter"
        raise ValueError(msg)


def _absolute(response: httpx2.Response, target: str) -> PageRequest:
    """Return the request for *target*, resolved against the URL that sent *response*, as an absolute URL."""
    return PageRequest(str(response.url.join(target)))


@dataclass(frozen=True, slots=True, kw_only=True)
class Cursor[P, I]:
    """Pages linked by an opaque cursor: ``next`` reads it from a page, and it is sent back as the query ``param``.

    The last page is the one whose ``next`` returns ``None`` or ``""``, or one with no items: a cursor that advances
    while returning nothing makes no progress. Set ``stop_on_empty=False`` for an endpoint that sends empty pages
    mid-walk.
    """

    items: Callable[[P], Sequence[I]]
    next: Callable[[P], str | None]
    param: str
    stop_on_empty: bool = True

    def __post_init__(self) -> None:
        """Refuse an empty parameter name."""
        _check_param(self.param)

    def first(self, request: PageRequest) -> PageRequest:
        """Return *request* unchanged: the first page is fetched without a cursor."""
        return request

    def page_items(self, page: P) -> Sequence[I]:
        """Return the items on *page*."""
        return self.items(page)

    def next_page(self, page: P, request: PageRequest, response: httpx2.Response) -> PageRequest | None:
        """Return *request* carrying *page*'s cursor, or ``None`` when it has none or (by default) no items."""
        del response
        if self.stop_on_empty and not self.items(page):
            return None
        cursor = self.next(page)
        return request.with_param(self.param, cursor) if bool(cursor) else None


@dataclass(frozen=True, slots=True, kw_only=True)
class Offset[P, I]:
    """Pages of at most ``page_size`` items, addressed by the offset of their first item, sent as the query ``param``.

    The offset starts at ``start`` and advances by the number of items each page held. A page holding fewer than
    ``page_size`` items (or none) is the last. The page size itself is the caller's to send, in ``params=`` under
    whatever name the endpoint uses, since that name varies (``limit``, ``per_page``, ``page_size``).
    """

    items: Callable[[P], Sequence[I]]
    page_size: int
    param: str = "offset"
    start: int = 0

    def __post_init__(self) -> None:
        """Refuse a page size below one, a negative start and an empty parameter name."""
        if self.page_size < 1:
            msg = f"page_size must be >= 1 (got {self.page_size})"
            raise ValueError(msg)
        if self.start < 0:
            msg = f"start must be >= 0 (got {self.start})"
            raise ValueError(msg)
        _check_param(self.param)

    def first(self, request: PageRequest) -> PageRequest:
        """Return *request* at offset ``start``."""
        return request.with_param(self.param, self.start)

    def page_items(self, page: P) -> Sequence[I]:
        """Return the items on *page*."""
        return self.items(page)

    def next_page(self, page: P, request: PageRequest, response: httpx2.Response) -> PageRequest | None:
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
        """Return *request* unchanged."""
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
        """Return *request* unchanged."""
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
