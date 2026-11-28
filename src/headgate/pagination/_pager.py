# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""How a paginated endpoint links its pages: the :class:`Pager` protocol and its strategies.

A pager is a frozen value that knows three things about a page ``P`` it never fetches itself: how it is read (its
``read``, a :class:`~headgate.Reader` or :class:`~headgate.Expect`, the pager's first argument), the items ``I`` on it,
and the request for the page after it. Since the reader comes first, a type checker knows ``P`` before it reads the
lambdas after it: ``Cursor(reader(Page), items=lambda p: p.items, ...)`` is a ``Cursor[Item, Page]``, and a typo in a
lambda is a type error. The client does the fetching (:meth:`Client.paginate
<headgate.Client.paginate>`), so every page goes through the whole stack.

* :class:`Cursor`: the page names an opaque cursor, sent back as a query parameter on the same URL.
* :class:`Offset`: the next page starts where this one ended, as an offset query parameter.
* :class:`PageNumber`: the next page is the page number after this one, as a query parameter.
* :class:`LinkHeader`: the response's ``Link`` header names the next page (RFC 8288 ``rel="next"``).
* :class:`NextUrl`: the page's body names the next page's URL.
* :class:`BodyCursor`: like :class:`Cursor`, but the cursor is put into the POSTed request body.
* :class:`BodyOffset`: like :class:`Offset`, but the offset is put into the POSTed request body.

A pager returns a next-page URL from the wire as an ``httpx2.URL``, absolute or relative, and the walk resolves it
against the URL that answered and sends it as an absolute URL, which the client treats like any other: credentials go
only to the origins the client trusts. A next page that would take an ``https`` walk down to plain ``http`` is refused,
as a redirect would be. Both are the walk's, so they hold for every pager.
"""

from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import KW_ONLY, dataclass, replace
from types import MappingProxyType
from typing import ClassVar, Final, Protocol, override

import httpx2

from headgate._core import check_callable, check_int, check_kind
from headgate._request_body import Writer, query_values
from headgate._responses import Expect, Reader
from headgate._url import CallUrl
from headgate.pagination._link import next_link

__all__ = [
    "BodyCursor",
    "BodyOffset",
    "Cursor",
    "LinkHeader",
    "NextUrl",
    "Offset",
    "PageNumber",
    "PageRequest",
    "Pager",
    "WritingPager",
]


@dataclass(frozen=True, slots=True)
class PageRequest[B = None]:
    """The request for one page, as a :class:`Pager` reads and returns it: its URL (as a verb takes it), its params.

    ``paginate`` builds the first from its URL, ``params=`` and ``payload=``; a pager derives each next one from it.

    A request with no ``payload`` is a GET, and one with a ``payload`` a POST. The payload stays the caller's own
    value, of type ``B``, so that a pager can rewrite it for the next page; how it is sent as the request body is the
    walk's, not the request's (see :class:`WritingPager`).
    """

    url: CallUrl
    params: Mapping[str, object] | None = None
    payload: B | None = None

    def __post_init__(self) -> None:
        """Freeze the params."""
        if self.params is not None:
            object.__setattr__(self, "params", MappingProxyType(dict(query_values(self.params))))

    def with_param(self, name: str, value: object) -> PageRequest[B]:
        """Return this request with the query parameter *name* set to *value*."""
        return replace(self, params={**(self.params or {}), name: value})

    def with_payload(self, payload: B) -> PageRequest[B]:
        """Return this request with *payload* as its payload."""
        return replace(self, payload=payload)


class Pager[I, P, B = None](Protocol):
    """How pages of type ``P`` holding items of type ``I``, fetched with request bodies of type ``B``, link up.

    ``read`` reads each page. The item type comes first, as in :class:`~headgate.Paginated`.

    ``B`` is the type of the walk's body, ``None`` for a pager that only walks GETs. A pager typed with a body may walk
    GETs too, when ``paginate`` is given no payload (``payload=None``): the first request's ``payload`` is then
    ``None``, and a later one has a body only if the pager gives it one.

    A pager whose next request is computed by counting (an offset, a page number) rather than read from the page or
    the response declares ``counts_pages: ClassVar[bool] = True`` (see :attr:`WritingPager.counts_pages`); it is not a
    member of this protocol, so a pager that reads its next request needs nothing.
    """

    __slots__ = ()

    @property
    def read(self) -> Reader[P] | Expect[P]:
        """The reader of every page: a :class:`~headgate.Reader`, or an :class:`~headgate.Expect` of its answers."""
        ...

    def first(self, request: PageRequest[B]) -> PageRequest[B]:
        """Return the request for the first page, given the one the caller wrote.

        It runs once, when :meth:`~headgate.Client.paginate` builds the walk, and every iteration starts from
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


class WritingPager[I, P, B](ABC):
    """A :class:`Pager` that writes the walk's request bodies: ``write`` encodes the walk's ``payload=`` for every page.

    It is a ``Pager`` by its shape, and does not subclass the protocol, whose ``read`` property a dataclass subclass
    would take as its field's default.

    The body is the caller's own value, which ``write`` encodes; with ``write`` ``None``, the value is builtins, sent as
    JSON. :class:`Cursor`, :class:`Offset`,
    :class:`BodyCursor` and :class:`BodyOffset` are writing pagers; a pager of GETs is not one.

    A pager is one by subclassing this class, not by its shape: an attribute that only shares the name ``write`` is
    never taken for a writer. Its methods are abstract, so a subclass that leaves one out cannot be built. ``read``
    (the reader of every page, see :attr:`Pager.read`) and ``write`` (the flavor's ``writer`` that encodes each page's
    body, or ``None`` for one sent as JSON) are declared here and given by the subclass, as dataclass fields or
    properties; :meth:`~headgate.Client.paginate` refuses a writing pager without either.
    """

    __slots__ = ()

    read: Reader[P] | Expect[P]
    write: Writer[B] | None

    #: Whether the pager's next request is computed without reading the page or the response: by counting, as an
    #: offset or a page number. A server that ignores such paging answers the same page forever under requests that
    #: never repeat, so the walk refuses a page with items whose body repeats an earlier page's. A pager that reads its
    #: next request (a cursor, a link) keeps the default ``False``: an identical body gives it an identical next
    #: request, which the walk's repeated-request check refuses, and two pages of identical data are walked. The walk
    #: reads it from any pager, by its shape too, defaulting to ``False``.
    counts_pages: ClassVar[bool] = False

    @abstractmethod
    def first(self, request: PageRequest[B]) -> PageRequest[B]:
        """Return the request for the first page, given the one the caller wrote (see :meth:`Pager.first`)."""

    @abstractmethod
    def page_items(self, page: P) -> Iterable[I]:
        """Return the items on *page*."""

    @abstractmethod
    def next_page(self, page: P, request: PageRequest[B], response: httpx2.Response) -> PageRequest[B] | None:
        """Return the request for the page after *page* (fetched by *request*, answered by *response*), or ``None``."""


#: What each pager hook takes, for the refusal of one that cannot be called.
_HOOKS: Final = {
    "items": "a function returning a page's items",
    "next": "a function returning a page's cursor or next URL",
    "more": "a function saying whether a page follows",
    "total": "a function returning the collection's exact size, or None",
    "offset": "a function returning a body's offset",
    "put": "a function returning the body for the next page",
    "write": "a writer, such as a flavor's writer(Query), or None to send builtins as JSON",
}


def _check_pager(pager: object) -> None:
    """Refuse a built-in *pager* whose reader, hooks, numbers or names are of the wrong kind, naming the field.

    A pager is a value built at import, so a page reader passed in the wrong place, or a hook that is not a function,
    fails there rather than at the first page of the first walk.
    """
    read: object = getattr(pager, "read", None)
    check_kind("read", read, (Reader, Expect), "the page reader, a Reader or an Expect, such as reader(Page)")
    for name, wanted in _HOOKS.items():
        if (hook := getattr(pager, name, None)) is not None:
            check_callable(name, hook, wanted)
    for name in ("page_size", "start"):
        if hasattr(pager, name):
            check_int(name, getattr(pager, name), "a number of items or pages, an int")
    check_kind("stop_on_empty", getattr(pager, "stop_on_empty", True), bool, "a bool")


def _check_param(param: str) -> None:
    """Refuse a query parameter name that is not a ``str``, or is empty."""
    check_kind("param", param, str, "the name of a query parameter, a str")
    if not param:
        msg = "param must name a query parameter"
        raise ValueError(msg)


def _last[P](page: P, count: int, page_size: int, total: Callable[[P], int | None] | None, walked: int) -> bool:
    """Return whether *page*, holding *count* items, is the last, *walked* items into the walk.

    With an exact total read from *page*, the walk ends when it has walked that many items or a page is empty, so a
    server that caps its pages below *page_size* is walked to the end; without one, a short page is the last.
    """
    known = None if total is None else total(page)
    if known is None:
        return count < page_size
    return count == 0 or walked >= known


def _position[B](request: PageRequest[B], param: str, default: int, what: str, *, minimum: int = 0) -> int:
    """Return the number *request* carries under *param* (an int, or a string of digits), else *default*.

    :raises ValueError: for a value that is neither, or is below *minimum*, naming it *what* (``"a page number"``,
        ``"an offset"``, ``"a page size"``).
    """
    value = (request.params or {}).get(param)
    if value is None:
        return default
    if isinstance(value, str) and value.isascii() and value.isdecimal() and int(value) >= minimum:
        return int(value)
    if isinstance(value, int) and not isinstance(value, bool) and value >= minimum:
        return value
    msg = f"{param} must be {what}, an int >= {minimum} or a string of its digits (got {value!r})"
    raise ValueError(msg)


def _wire_url(target: str) -> PageRequest:
    """Return the request for *target*, a next-page URL from the wire, which the walk resolves and vets."""
    return PageRequest(httpx2.URL(target))


def _check_get[B](request: PageRequest[B], pager: str) -> None:
    """Refuse a walk with a request body for *pager*, whose next page is a URL fetched with a GET."""
    if request.payload is not None:
        msg = f"{pager} follows next-page URLs with GETs, so its walk takes no request body"
        raise ValueError(msg)


def _posted[B](request: PageRequest[B], pager: str) -> B:
    """Return *request*'s body, refusing a request with none: *pager* rewrites the body, so its walk is POSTs."""
    if request.payload is None:
        msg = f"{pager} rewrites the request body, so its walk needs one: pass payload="
        raise ValueError(msg)
    return request.payload


@dataclass(frozen=True, slots=True)
class Cursor[I, P, B = None](WritingPager[I, P, B]):
    """Pages linked by an opaque cursor: ``next`` reads it from a page, and it is sent back as the query ``param``.

    The last page is the one whose ``next`` returns ``None`` or ``""``, or one with no items: a cursor that advances
    while returning nothing makes no progress. Set ``stop_on_empty=False`` for an endpoint that sends empty pages
    mid-walk. ``B`` is the type of the request body for a walk of POSTs (``Cursor[Item, Page, Query]``), and
    ``None`` for a walk of GETs. ``write`` encodes the walk's ``payload=`` on each page (a flavor's ``writer``); a walk
    of POSTs without one sends it as JSON, and a walk of GETs leaves it unused.
    """

    read: Reader[P] | Expect[P]
    _: KW_ONLY
    items: Callable[[P], Sequence[I]]
    next: Callable[[P], str | None]
    param: str
    write: Writer[B] | None = None
    stop_on_empty: bool = True

    def __post_init__(self) -> None:
        """Refuse a field of the wrong kind and an empty parameter name."""
        _check_pager(self)
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


@dataclass(frozen=True, slots=True)
class Offset[I, P, B = None](WritingPager[I, P, B]):
    """Pages of at most ``page_size`` items, addressed by the offset of their first item, sent as the query ``param``.

    ``start`` is the offset of the collection's first item (0, or 1 for a 1-based API). The walk starts at the offset
    the caller's own ``params=`` give under ``param`` (an int, or a string of digits), or at ``start`` when they give
    none, and advances by the number of items each page held. A page holding fewer than ``page_size`` items (or none)
    is the last. With ``total``, the number of items the whole collection holds, read from a page, the walk instead
    ends once it has passed that many items from ``start``, or at an empty page: a walk whose items fill its last page
    ends there instead of fetching an empty page past it, and one whose server caps pages below ``page_size`` is
    walked to the end. ``total`` returns ``None`` for a page that states no exact total (one reported as "at least"),
    which leaves the walk to end at a short page. ``B`` is the type of the request body for a walk of POSTs, and
    ``write`` its writer, as on :class:`Cursor`.

    The page size goes out under ``size_param``, whatever name the endpoint uses (``limit``, ``per_page``, SODA's
    ``$limit``). The caller's own size there, an int of at least 1 or a string of digits, is sent and stepped by;
    without one, ``page_size`` is sent. Without ``size_param``, the page size is the caller's to send in ``params=``,
    and the walk steps by ``page_size``.
    """

    counts_pages: ClassVar[bool] = True

    read: Reader[P] | Expect[P]
    _: KW_ONLY
    items: Callable[[P], Sequence[I]]
    page_size: int
    param: str = "offset"
    size_param: str | None = None
    start: int = 0
    total: Callable[[P], int | None] | None = None
    write: Writer[B] | None = None

    def __post_init__(self) -> None:
        """Refuse a field of the wrong kind, a page size below one, a negative start, and a bad parameter name.

        A parameter name is bad when it is empty, or when ``param`` and ``size_param`` name the same one.
        """
        _check_pager(self)
        if self.page_size < 1:
            msg = f"page_size must be >= 1 (got {self.page_size})"
            raise ValueError(msg)
        if self.start < 0:
            msg = f"start must be >= 0 (got {self.start})"
            raise ValueError(msg)
        _check_param(self.param)
        if self.size_param is not None:
            if not self.size_param:
                msg = "size_param must name a query parameter"
                raise ValueError(msg)
            if self.size_param == self.param:
                msg = f"size_param must differ from param: both name {self.param!r}"
                raise ValueError(msg)

    @override
    def first(self, request: PageRequest[B]) -> PageRequest[B]:
        """Return *request* at its own offset, or at ``start``, with its page size; refuse a value that is not one."""
        first = request.with_param(self.param, _position(request, self.param, self.start, "an offset"))
        if self.size_param is None:
            return first
        return first.with_param(self.size_param, self._size(request))

    @override
    def page_items(self, page: P) -> Sequence[I]:
        """Return the items on *page*."""
        return self.items(page)

    @override
    def next_page(self, page: P, request: PageRequest[B], response: httpx2.Response) -> PageRequest[B] | None:
        """Return *request* at the offset after *page*'s items, or ``None`` after the last page (see the class)."""
        del response
        count = len(self.items(page))
        following = _position(request, self.param, self.start, "an offset") + count
        if _last(page, count, self._size(request), self.total, following - self.start):
            return None
        return request.with_param(self.param, following)

    def _size(self, request: PageRequest[B]) -> int:
        """Return the page size *request* carries under ``size_param``, else ``page_size``; refuse one below 1."""
        if self.size_param is None:
            return self.page_size
        return _position(request, self.size_param, self.page_size, "a page size", minimum=1)


@dataclass(frozen=True, slots=True)
class PageNumber[I, P, B = None](WritingPager[I, P, B]):
    """Pages addressed by number, sent as the query ``param``: page *n* + 1 follows page *n* while ``more`` says so.

    The walk starts at the page number the caller's own ``params=`` give under ``param`` (an int, or a string of
    digits), or at ``start`` (1) when they give none, which is then sent. ``more`` reads from a page whether another
    follows it: a ``next`` link read as a yes or no, a page count, a ``has_more`` flag. Because the next request is
    the caller's own with the number advanced, every page keeps the caller's URL and query, even where the page links
    to its successor, and a host or path in such a link is never fetched. The last page is the one for which ``more``
    returns ``False``, or one with no items (``stop_on_empty=False`` asks for the page after an empty one). ``B`` is
    the type of the request body for a walk of POSTs, and ``write`` its writer, as on :class:`Cursor`.
    """

    counts_pages: ClassVar[bool] = True

    read: Reader[P] | Expect[P]
    _: KW_ONLY
    items: Callable[[P], Sequence[I]]
    more: Callable[[P], bool]
    param: str = "page"
    start: int = 1
    write: Writer[B] | None = None
    stop_on_empty: bool = True

    def __post_init__(self) -> None:
        """Refuse a field of the wrong kind, a negative start and an empty parameter name."""
        _check_pager(self)
        if self.start < 0:
            msg = f"start must be >= 0 (got {self.start})"
            raise ValueError(msg)
        _check_param(self.param)

    @override
    def first(self, request: PageRequest[B]) -> PageRequest[B]:
        """Return *request* at its own page number, or at ``start``, refusing a number that is not one."""
        return request.with_param(self.param, _position(request, self.param, self.start, "a page number"))

    @override
    def page_items(self, page: P) -> Sequence[I]:
        """Return the items on *page*."""
        return self.items(page)

    @override
    def next_page(self, page: P, request: PageRequest[B], response: httpx2.Response) -> PageRequest[B] | None:
        """Return *request* at the next page number, or ``None`` when ``more`` says none or *page* is empty."""
        del response
        if self.stop_on_empty and not self.items(page):
            return None
        if not self.more(page):
            return None
        return request.with_param(self.param, _position(request, self.param, self.start, "a page number") + 1)


@dataclass(frozen=True, slots=True)
class LinkHeader[I, P]:
    """Pages linked by the response's ``Link`` header: the target of its ``rel="next"`` link is the next page.

    The last page is the one with no ``rel="next"`` link, or one with no items (``stop_on_empty=False`` follows the
    link past an empty page).
    """

    read: Reader[P] | Expect[P]
    _: KW_ONLY
    items: Callable[[P], Sequence[I]]
    stop_on_empty: bool = True

    def __post_init__(self) -> None:
        """Refuse a field of the wrong kind."""
        _check_pager(self)

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
        target = next_link(response.headers, response.url)
        return None if target is None else _wire_url(target)


@dataclass(frozen=True, slots=True)
class NextUrl[I, P]:
    """Pages linked by a URL in the body: ``next`` reads the next page's URL (absolute or relative) from a page.

    The last page is the one whose ``next`` returns ``None`` or ``""``, or one with no items (``stop_on_empty=False``
    follows the URL past an empty page).
    """

    read: Reader[P] | Expect[P]
    _: KW_ONLY
    items: Callable[[P], Sequence[I]]
    next: Callable[[P], str | None]
    stop_on_empty: bool = True

    def __post_init__(self) -> None:
        """Refuse a field of the wrong kind."""
        _check_pager(self)

    def first(self, request: PageRequest) -> PageRequest:
        """Return *request* unchanged, refusing one with a body: a next URL is fetched with a GET."""
        _check_get(request, "NextUrl")
        return request

    def page_items(self, page: P) -> Sequence[I]:
        """Return the items on *page*."""
        return self.items(page)

    def next_page(self, page: P, request: PageRequest, response: httpx2.Response) -> PageRequest | None:
        """Return the request for *page*'s next URL, or ``None`` when it names none or (by default) has no items."""
        del request, response
        if self.stop_on_empty and not self.items(page):
            return None
        target = self.next(page)
        return _wire_url(target) if bool(target) else None


@dataclass(frozen=True, slots=True)
class BodyCursor[I, P, B](WritingPager[I, P, B]):
    """Pages linked by an opaque cursor carried in the POSTed body: ``put`` writes it into the next page's body.

    For an endpoint that takes its search as a request body (a search API, a JSON-RPC call): the walk POSTs the
    ``payload=`` the caller passed ``paginate``, and each next page POSTs ``put(body, cursor)``, the same body carrying
    the cursor ``next`` read from the page. ``put`` returns a new body, leaving the one it was given as it was.
    ``write`` encodes each page's body (a flavor's ``writer``); without it the body is sent as JSON, so it is builtins.
    The last page is the one whose ``next`` returns ``None`` or ``""``, or one with no items (``stop_on_empty=False``
    follows the cursor past an empty page).
    """

    read: Reader[P] | Expect[P]
    _: KW_ONLY
    items: Callable[[P], Sequence[I]]
    next: Callable[[P], str | None]
    put: Callable[[B, str], B]
    write: Writer[B] | None = None
    stop_on_empty: bool = True

    def __post_init__(self) -> None:
        """Refuse a field of the wrong kind."""
        _check_pager(self)

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
        return request.with_payload(self.put(_posted(request, "BodyCursor"), cursor))


@dataclass(frozen=True, slots=True)
class BodyOffset[I, P, B](WritingPager[I, P, B]):
    """Pages of at most ``page_size`` items, addressed by the offset of their first item, carried in the POSTed body.

    ``offset`` reads the offset from a body and ``put`` returns a new body at another one. The walk starts at the
    offset the first request's body carries, and advances by the number of items each page held. A page holding fewer
    than ``page_size`` items (or none) is the last; with ``total``, the walk ends as on :class:`Offset`, counting from
    offset 0. The
    page size itself is the caller's to put in the body, as it is the caller's to put in :class:`Offset`'s query.
    ``write`` encodes each page's body, as for :class:`BodyCursor`.
    """

    counts_pages: ClassVar[bool] = True

    read: Reader[P] | Expect[P]
    _: KW_ONLY
    items: Callable[[P], Sequence[I]]
    page_size: int
    offset: Callable[[B], int]
    put: Callable[[B, int], B]
    total: Callable[[P], int | None] | None = None
    write: Writer[B] | None = None

    def __post_init__(self) -> None:
        """Refuse a field of the wrong kind and a page size below one."""
        _check_pager(self)
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
        """Return *request*'s body at the offset past *page*'s items, or ``None`` after a short page or at ``total``."""
        del response
        count = len(self.items(page))
        body = _posted(request, "BodyOffset")
        following = self.offset(body) + count
        if _last(page, count, self.page_size, self.total, following):
            return None
        return request.with_payload(self.put(body, following))
