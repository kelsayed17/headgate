# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""``AsyncBaseClient.paginate``: each strategy, laziness, the full stack per page, and termination."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, assert_type

import httpx2
import msgspec
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from headgate import (
    ClientError,
    Cursor,
    Expect,
    HttpOptions,
    LinkHeader,
    NextUrl,
    Offset,
    PageLimit,
    PageLimitError,
    RequestOptions,
    ResponseParseError,
    UpstreamStatusError,
)
from headgate.msgspec import decoder
from headgate.pagination import PageRequest
from headgate.ratelimit import Rate
from tests.support.clients import StubClient

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from headgate.pagination import Pager

_BASE = "https://api.test"
_RATE = Rate.per_second(100)


class Item(msgspec.Struct, frozen=True):
    """One item on a page."""

    id: int


class Page(msgspec.Struct, frozen=True):
    """A page of items, naming the next page by cursor or URL."""

    data: list[Item]
    cursor: str | None = None
    next: str | None = None


def items_of(page: Page) -> list[Item]:
    """Return the items on *page*."""
    return page.data


def cursor_of(page: Page) -> str | None:
    """Return *page*'s cursor."""
    return page.cursor


def next_of(page: Page) -> str | None:
    """Return *page*'s next URL."""
    return page.next


@dataclass
class _Api:
    """An in-memory API: a function of the request answering each page, recording every request it was sent."""

    answer: Callable[[httpx2.Request], httpx2.Response]
    sent: list[httpx2.URL] = field(default_factory=list)

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        """Record *request* and answer it."""
        self.sent.append(request.url)
        return self.answer(request)

    def client(self) -> StubClient:
        """Return a client over this API."""
        return StubClient(base_url=_BASE, rate=_RATE, http=HttpOptions(transport=httpx2.MockTransport(self.handle)))


def _page(request: httpx2.Request, ids: Sequence[int], **links: str | None) -> httpx2.Response:
    """Answer *request* with a page of *ids* and the given cursor/next links."""
    body = msgspec.json.encode(Page([Item(i) for i in ids], **links))
    return httpx2.Response(200, content=body, request=request)


def _by_cursor(request: httpx2.Request) -> httpx2.Response:
    """Serve three pages linked by cursors ``b`` and ``c``."""
    cursor = request.url.params.get("after")
    pages = {None: ([1, 2], "b"), "b": ([3], "c"), "c": ([4], None)}
    ids, following = pages[cursor]
    return _page(request, ids, cursor=following)


async def test_cursor_pages_are_followed_and_items_are_typed() -> None:
    """Follow cursors as a query parameter on the same URL, keeping the caller's other parameters."""
    api = _Api(_by_cursor)
    paginated = api.client().paginate(
        t"v1/{'items'}",
        expect=decoder(Page),
        pages=Cursor(items=items_of, next=cursor_of, param="after"),
        params={"kind": "a"},
    )
    items = [item async for item in paginated]
    assert_type(items, list[Item])
    assert [item.id for item in items] == [1, 2, 3, 4]
    assert [str(url) for url in api.sent] == [
        f"{_BASE}/v1/items?kind=a",
        f"{_BASE}/v1/items?kind=a&after=b",
        f"{_BASE}/v1/items?kind=a&after=c",
    ]


async def test_iteration_is_lazy_and_restarts_from_the_first_page() -> None:
    """Fetch nothing before iteration, only what the iteration reached, and start again on a new iteration."""
    api = _Api(_by_cursor)
    paginated = api.client().paginate(
        "items", expect=decoder(Page), pages=Cursor(items=items_of, next=cursor_of, param="after")
    )
    assert api.sent == []
    async for item in paginated:
        assert item.id == 1
        break
    assert len(api.sent) == 1
    pages = [page async for page in paginated.pages()]
    assert [len(page.data) for page in pages] == [2, 1, 1]
    assert len(api.sent) == 1 + len(pages)


async def test_offset_pages_advance_by_the_items_received_until_a_short_page() -> None:
    """Advance the offset by each page's item count, and stop at a page shorter than the limit."""
    total = 5

    def answer(request: httpx2.Request) -> httpx2.Response:
        offset = int(request.url.params["offset"])
        return _page(request, [i for i in range(offset, offset + 2) if i < total])

    api = _Api(answer)
    pages = Offset(items=items_of, page_size=2, start=0)
    items = [
        item.id async for item in api.client().paginate("items", expect=decoder(Page), pages=pages, params={"limit": 2})
    ]
    assert items == [0, 1, 2, 3, 4]
    assert [url.params["offset"] for url in api.sent] == ["0", "2", "4"]


async def test_offset_stops_at_an_empty_page_and_counts_from_start() -> None:
    """Stop at an empty page, the first request carrying the configured start."""
    end = 12

    def answer(request: httpx2.Request) -> httpx2.Response:
        skip = int(request.url.params["skip"])
        return _page(request, [skip] if skip < end else [])

    api = _Api(answer)
    pages = Offset(items=items_of, page_size=1, param="skip", start=10)
    assert [item.id async for item in api.client().paginate("items", expect=decoder(Page), pages=pages)] == [10, 11]
    assert [url.params["skip"] for url in api.sent] == ["10", "11", "12"]


def test_offset_resumes_from_start_when_the_request_lacks_its_parameter() -> None:
    """Treat a request without the offset parameter as being at ``start``."""
    pages = Offset(items=items_of, page_size=1, start=3)
    following = pages.next_page(Page([Item(1)]), PageRequest("items"), httpx2.Response(200))
    assert following == PageRequest("items", {"offset": 4})


async def test_link_header_pages_are_followed_as_absolute_urls() -> None:
    """Follow ``rel="next"``, resolved against the answering URL, without re-sending the first page's query."""

    def answer(request: httpx2.Request) -> httpx2.Response:
        page = int(request.url.params.get("page", "1"))
        links = {1: '</items?page=2>; rel="next"', 2: '<https://cdn.test/items?page=3>; rel="next"', 3: ""}
        return httpx2.Response(200, json={"data": [{"id": page}]}, headers={"link": links[page]}, request=request)

    api = _Api(answer)
    paginated = api.client().paginate(
        "items", expect=decoder(Page), pages=LinkHeader(items=items_of), params={"q": "x"}
    )
    assert [item.id async for item in paginated] == [1, 2, 3]
    assert [str(url) for url in api.sent] == [
        f"{_BASE}/items?q=x",
        f"{_BASE}/items?page=2",
        "https://cdn.test/items?page=3",
    ]


async def test_next_url_pages_are_followed_from_the_body() -> None:
    """Follow a next URL the page names, relative or absolute."""

    def answer(request: httpx2.Request) -> httpx2.Response:
        following = {"/a": "b", "/b": f"{_BASE}/c", "/c": None}[request.url.path]
        return _page(request, [len(request.url.path)], next=following)

    api = _Api(answer)
    paginated = api.client().paginate("a", expect=decoder(Page), pages=NextUrl(items=items_of, next=next_of))
    assert len([item async for item in paginated]) == len(("a", "b", "c"))
    assert [url.path for url in api.sent] == ["/a", "/b", "/c"]


async def test_each_page_goes_through_the_stack() -> None:
    """Retry a page that fails transiently, and raise a page that fails for good, after the earlier items."""
    statuses = iter((200, 503, 200, 404))

    def answer(request: httpx2.Request) -> httpx2.Response:
        status = next(statuses)
        if status != httpx2.codes.OK:
            return httpx2.Response(status, request=request)
        return _by_cursor(request)

    received: list[int] = []
    paginated = (
        _Api(answer)
        .client()
        .paginate("items", expect=decoder(Page), pages=Cursor(items=items_of, next=cursor_of, param="after"))
    )

    async def collect(record: Callable[[int], None]) -> None:
        async for item in paginated:
            record(item.id)

    with pytest.raises(UpstreamStatusError) as raised:
        await collect(received.append)
    assert raised.value.status_code == httpx2.codes.NOT_FOUND
    assert received == [1, 2, 3]


async def test_every_page_carries_the_walks_cache_ttl() -> None:
    """Stamp ``cache_ttl=`` on every page's GET, as on one ``get``."""
    ttl = 30.0
    stamped: list[object] = []

    def answer(request: httpx2.Request) -> httpx2.Response:
        stamped.append(request.extensions.get("hishel_ttl"))
        return _by_cursor(request)

    pages = Cursor(items=items_of, next=cursor_of, param="after")
    paginated = _Api(answer).client().paginate("items", expect=decoder(Page), pages=pages, cache_ttl=ttl)
    assert len([item async for item in paginated]) == len((1, 2, 3, 4))
    assert stamped == [ttl, ttl, ttl]


async def test_a_walk_that_does_not_follow_redirects_raises_a_pages_redirect() -> None:
    """Raise a page's redirect as its 3xx with ``follow_redirects=False``, as on one ``get``."""

    def answer(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/moved":
            return httpx2.Response(302, headers={"Location": f"{_BASE}/items"}, request=request)
        return _by_cursor(request)

    pages = Cursor(items=items_of, next=cursor_of, param="after")
    api = _Api(answer)
    with pytest.raises(UpstreamStatusError) as raised:
        await api.client().paginate("moved", expect=decoder(Page), pages=pages, follow_redirects=False).collect()
    assert raised.value.status_code == httpx2.codes.FOUND
    assert [url.path for url in api.sent] == ["/moved"]


async def test_every_page_sends_the_expects_headers_and_accept() -> None:
    """Send an ``Expect``'s ``headers`` and ``accept`` on every page, as one ``get`` does."""
    sent: list[httpx2.Request] = []

    def answer(request: httpx2.Request) -> httpx2.Response:
        sent.append(request)
        page = _by_cursor(request)
        return httpx2.Response(200, json=msgspec.json.decode(page.content), request=request)

    expect = Expect(decoder(Page), headers={"X-Api-Version": "2"}, accept="application/json")
    pages = Cursor(items=items_of, next=cursor_of, param="after")
    assert len(await _Api(answer).client().paginate("items", expect=expect, pages=pages).collect()) == len((1, 2, 3, 4))
    assert [(r.headers["X-Api-Version"], r.headers["Accept"]) for r in sent] == [("2", "application/json")] * 3


async def test_a_page_of_another_media_type_than_the_expect_accepts_raises() -> None:
    """Refuse a page whose ``Content-Type`` the ``Expect`` does not accept, before its reader runs."""

    def answer(request: httpx2.Request) -> httpx2.Response:
        page = _by_cursor(request)
        return httpx2.Response(200, content=page.content, headers={"Content-Type": "text/html"}, request=request)

    expect = Expect(decoder(Page), accept="application/json")
    paginated = _Api(answer).client().paginate("items", expect=expect, pages=LinkHeader(items=items_of))
    with pytest.raises(ResponseParseError, match="text/html"):
        await paginated.collect()


async def test_a_declared_answer_is_a_page() -> None:
    """Read a status the call declared with ``Expect.on`` as a page."""

    def answer(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(404, request=request)

    expect = Expect(decoder(Page)).on(404, lambda _resp: Page([]))
    paginated = _Api(answer).client().paginate("items", expect=expect, pages=LinkHeader(items=items_of))
    assert [page async for page in paginated.pages()] == [Page([])]


async def test_a_page_that_leads_back_raises_instead_of_looping() -> None:
    """Raise ``ClientError`` when a next URL repeats an earlier request."""

    def answer(request: httpx2.Request) -> httpx2.Response:
        return _page(request, [1], next="/items")

    paginated = (
        _Api(answer).client().paginate("items", expect=decoder(Page), pages=NextUrl(items=items_of, next=next_of))
    )
    with pytest.raises(ClientError, match="pagination loop") as raised:
        _ = [item async for item in paginated]
    assert raised.value.url == f"{_BASE}/items"
    assert raised.value.source == "StubClient"
    assert raised.value.method == "GET"
    assert str(raised.value) == (
        f"[StubClient] pagination loop: the page after page 2 repeats an earlier page's request ({_BASE}/items)"
    )


async def test_a_repeating_cursor_on_a_template_url_raises() -> None:
    """Raise when a cursor repeats on a templated URL, without naming the URL."""

    def answer(request: httpx2.Request) -> httpx2.Response:
        return _page(request, [1], cursor="same")

    pages = Cursor(items=items_of, next=cursor_of, param="after")
    paginated = _Api(answer).client().paginate(t"items/{'x'}", expect=decoder(Page), pages=pages)
    with pytest.raises(ClientError, match="pagination loop") as raised:
        _ = [page async for page in paginated.pages()]
    assert raised.value.url is None
    assert str(raised.value) == "[StubClient] pagination loop: the page after page 2 repeats an earlier page's request"


#: One request body of each kind no pager can rewrite.
_BODIES: list[RequestOptions] = [{"data": {}}, {"content": b"x"}, {"files": {"f": b"x"}}]


@pytest.mark.parametrize("body", _BODIES, ids=["data", "content", "files"])
def test_a_body_no_pager_can_rewrite_is_refused(body: RequestOptions) -> None:
    """Refuse a form, raw or multipart body: a walk's body is ``json=``, or ``body=`` with ``write=``."""
    client = _Api(_by_cursor).client()
    with pytest.raises(ValueError, match="takes no data=, content= or files="):
        client.paginate("items", expect=decoder(Page), pages=LinkHeader(items=items_of), **body)  # ty: ignore[no-matching-overload]


@pytest.mark.parametrize(
    "build",
    [
        lambda: Cursor(items=items_of, next=cursor_of, param=""),
        lambda: Offset(items=items_of, page_size=0),
        lambda: Offset(items=items_of, page_size=1, start=-1),
        lambda: Offset(items=items_of, page_size=1, param=""),
    ],
    ids=["empty-cursor-param", "zero-page-size", "negative-start", "empty-offset-param"],
)
def test_a_strategy_refuses_a_bad_configuration(build: Callable[[], object]) -> None:
    """Refuse a strategy that could not page."""
    with pytest.raises(ValueError, match="must"):
        build()


#: The pages of a random API: page *i* names page ``links[i]`` next, or none.
_GRAPHS = st.integers(min_value=1, max_value=8).flatmap(
    lambda size: st.lists(st.one_of(st.none(), st.integers(0, size - 1)), min_size=size, max_size=size)
)


class _Graph(httpx2.MockTransport):
    """A transport serving a page graph: page *i* holds item *i* and names ``links[i]`` as its cursor."""

    def __init__(self, links: list[int | None]) -> None:
        """Serve *links*."""
        self.fetched = 0
        self._links = links
        super().__init__(self._answer)

    def _answer(self, request: httpx2.Request) -> httpx2.Response:
        self.fetched += 1
        index = int(request.url.params.get("at", "0"))
        following = self._links[index]
        return _page(request, [index], cursor=None if following is None else str(following))


@settings(max_examples=60, deadline=None)
@given(_GRAPHS)
def test_pagination_terminates_for_any_page_graph(links: list[int | None]) -> None:
    """End every walk: at a page with no next, or with ``ClientError`` once a cursor repeats.

    Each page is fetched once, except that the first page, fetched without a cursor, is fetched again when a cursor
    names it; the cursor repeating after that ends the walk.
    """
    graph = _Graph(links)
    client = StubClient(base_url=_BASE, rate=_RATE, http=HttpOptions(transport=graph))
    paginated = client.paginate("items", expect=decoder(Page), pages=Cursor(items=items_of, next=cursor_of, param="at"))

    path = [0]
    while (following := links[path[-1]]) is not None and following not in path:
        path.append(following)
    cycles = following is not None

    async def walk() -> list[int]:
        return [item.id async for item in paginated]

    if cycles:
        with pytest.raises(ClientError, match="pagination loop"):
            asyncio.run(walk())
    else:
        assert asyncio.run(walk()) == path
    assert graph.fetched == len(path) + (following == 0)


def test_a_cursor_needs_a_parameter_name() -> None:
    """Refuse a cursor pager with no query parameter to send the cursor in, saying so."""
    with pytest.raises(ValueError, match=r"^param must name a query parameter$"):
        Cursor(items=items_of, next=cursor_of, param="")


# Walks that would never end on their own.

#: Pages a bounded test walk is allowed.
_BUDGET = 3
#: The last page of the walk that has an empty page in it.
_LAST = 2


def _linked(request: httpx2.Request, ids: list[int], following: int | None) -> httpx2.Response:
    """Answer *request* with *ids*, linking to page *following* by cursor, next URL and ``Link`` header alike."""
    if following is None:
        return _page(request, ids)
    response = _page(request, ids, cursor=str(following), next=f"/items?at={following}")
    response.headers["Link"] = f'</items?at={following}>; rel="next"'
    return response


def _endless(request: httpx2.Request) -> httpx2.Response:
    """Answer every page with one item and a link to a new page, so the walk never ends on its own."""
    at = int(request.url.params.get("at", "0"))
    return _linked(request, [at], at + 1)


def _with_an_empty_page(request: httpx2.Request) -> httpx2.Response:
    """Answer page 0 with an item, page 1 with none and page 2 with an item; pages 0 and 1 link to the next."""
    at = int(request.url.params.get("at", "0"))
    return _linked(request, [] if at == 1 else [at], None if at == _LAST else at + 1)


def _cursor(*, stop_on_empty: bool = True) -> Pager[Page, Item]:
    """Return a cursor strategy for the linked pages."""
    return Cursor(items=items_of, next=cursor_of, param="at", stop_on_empty=stop_on_empty)


def _next_url(*, stop_on_empty: bool = True) -> Pager[Page, Item]:
    """Return a next-URL strategy for the linked pages."""
    return NextUrl(items=items_of, next=next_of, stop_on_empty=stop_on_empty)


def _link_header(*, stop_on_empty: bool = True) -> Pager[Page, Item]:
    """Return a ``Link`` header strategy for the linked pages."""
    return LinkHeader(items=items_of, stop_on_empty=stop_on_empty)


_LINKED_STRATEGIES = [_cursor, _next_url, _link_header]
_LINKED_IDS = ["cursor", "next-url", "link-header"]


@pytest.mark.parametrize("strategy", _LINKED_STRATEGIES, ids=_LINKED_IDS)
async def test_a_page_with_no_items_ends_the_walk(strategy: Callable[..., Pager[Page, Item]]) -> None:
    """Stop at a page with no items, though it links to another: a link that advances while returning nothing."""
    api = _Api(_with_an_empty_page)
    paginated = api.client().paginate("items", expect=decoder(Page), pages=strategy())

    assert [item.id async for item in paginated] == [0]
    assert len(api.sent) == len(("the page with an item", "the empty page"))


@pytest.mark.parametrize("strategy", _LINKED_STRATEGIES, ids=_LINKED_IDS)
async def test_an_empty_page_can_be_walked_past(strategy: Callable[..., Pager[Page, Item]]) -> None:
    """Follow the link past an empty page with ``stop_on_empty=False``, for an endpoint that sends them mid-walk."""
    api = _Api(_with_an_empty_page)
    paginated = api.client().paginate("items", expect=decoder(Page), pages=strategy(stop_on_empty=False))

    assert [item.id async for item in paginated] == [0, _LAST]
    assert len(api.sent) == _LAST + 1


async def test_a_walk_past_its_page_limit_raises_with_what_it_fetched() -> None:
    """Raise ``PageLimitError`` once the budget is spent with pages to come, after yielding the pages it allowed."""
    api = _Api(_endless)
    paginated = api.client().paginate("items", expect=decoder(Page), pages=_cursor(), limit=PageLimit(_BUDGET))
    items = aiter(paginated)

    allowed = [(await anext(items)).id for _ in range(_BUDGET)]
    with pytest.raises(PageLimitError) as raised:
        await anext(items)

    assert allowed == list(range(_BUDGET)), "the pages the budget allowed were yielded before the raise"
    assert len(api.sent) == _BUDGET, "the page past the limit was never fetched"
    assert raised.value.pages == _BUDGET
    assert isinstance(raised.value, ClientError)
    assert (raised.value.method, raised.value.url) == ("GET", "items")
    assert str(raised.value) == (
        f"[StubClient] pagination reached its limit of {_BUDGET} pages with more to come (items); the result is"
        " incomplete. Raise PageLimit.max_pages, or set on_limit='stop' to end the walk there"
    )


async def test_a_limit_on_a_template_url_raises_without_naming_it() -> None:
    """Raise past the limit on a templated URL, without naming the URL."""
    client = _Api(_endless).client()
    paginated = client.paginate(t"items/{'x'}", expect=decoder(Page), pages=_cursor(), limit=PageLimit(1))
    with pytest.raises(PageLimitError) as raised:
        _ = [page async for page in paginated.pages()]
    assert (raised.value.method, raised.value.url) == ("GET", None)
    assert str(raised.value) == (
        "[StubClient] pagination reached its limit of 1 pages with more to come; the result is incomplete."
        " Raise PageLimit.max_pages, or set on_limit='stop' to end the walk there"
    )


async def test_a_walk_can_stop_quietly_at_its_page_limit() -> None:
    """End the walk at the budget with ``on_limit="stop"``, for a caller that wants only the first pages."""
    api = _Api(_endless)
    limit = PageLimit(_BUDGET, on_limit="stop")
    paginated = api.client().paginate("items", expect=decoder(Page), pages=_cursor(), limit=limit)

    assert [item.id async for item in paginated] == list(range(_BUDGET))
    assert len(api.sent) == _BUDGET


async def test_a_walk_that_ends_exactly_at_its_limit_is_complete() -> None:
    """Raise nothing when the last page is the budget's last: no page was left unfetched."""
    paginated = (
        _Api(_by_cursor)
        .client()
        .paginate(
            "items",
            expect=decoder(Page),
            pages=Cursor(items=items_of, next=cursor_of, param="after"),
            limit=PageLimit(3),
        )
    )
    assert [item.id async for item in paginated] == [1, 2, 3, 4]


@pytest.mark.parametrize(
    ("build", "message"),
    [
        (lambda: PageLimit(0), "max_pages must be >= 1 (got 0)"),
        (lambda: PageLimit(max_pages=True), "max_pages must be >= 1 (got True)"),
        (lambda: replace(PageLimit(5), on_limit="truncate"), "on_limit must be 'raise' or 'stop' (got 'truncate')"),
    ],
    ids=["zero", "a-bool", "unknown-outcome"],
)
def test_a_bad_page_limit_is_refused(build: Callable[[], object], message: str) -> None:
    """Refuse a budget below one page, a ``bool`` passed as a count, and an outcome that does not exist."""
    with pytest.raises(ValueError, match=f"^{re.escape(message)}$"):
        build()


async def test_collect_returns_every_item_in_order() -> None:
    """Collect the whole walk into a list, typed by the pager's items."""
    api = _Api(_by_cursor)
    paginated = api.client().paginate(
        "items", expect=decoder(Page), pages=Cursor(items=items_of, next=cursor_of, param="after")
    )
    items = await paginated.collect()
    assert_type(items, list[Item])
    assert [item.id for item in items] == [1, 2, 3, 4]
    assert len(api.sent) == len(("first page", "after=b", "after=c"))


@pytest.mark.parametrize(
    ("max_items", "ids", "fetched"),
    [
        (1, [1], 1),
        (2, [1, 2], 1),
        (3, [1, 2, 3], 2),
        (4, [1, 2, 3, 4], 3),
        (10, [1, 2, 3, 4], 3),
    ],
    ids=["inside-a-page", "fills-a-page", "fills-the-next", "everything", "more-than-there-is"],
)
async def test_collect_stops_at_max_items_without_fetching_further(
    max_items: int, ids: list[int], fetched: int
) -> None:
    """Return at most *max_items* items, fetching no page past the one that filled the list."""
    api = _Api(_by_cursor)
    paginated = api.client().paginate(
        "items", expect=decoder(Page), pages=Cursor(items=items_of, next=cursor_of, param="after")
    )
    assert [item.id for item in await paginated.collect(max_items=max_items)] == ids
    assert len(api.sent) == fetched


async def test_collect_of_no_items_sends_nothing() -> None:
    """Return an empty list for ``max_items=0`` without a request."""
    api = _Api(_endless)
    paginated = api.client().paginate("items", expect=decoder(Page), pages=_cursor())
    assert await paginated.collect(max_items=0) == []
    assert api.sent == []


async def test_collect_within_the_page_limit_raises_nothing() -> None:
    """Return the items when they are all collected before the page budget is spent, though more pages follow."""
    api = _Api(_endless)
    paginated = api.client().paginate("items", expect=decoder(Page), pages=_cursor(), limit=PageLimit(_BUDGET))
    assert [item.id for item in await paginated.collect(max_items=_BUDGET)] == list(range(_BUDGET))
    assert len(api.sent) == _BUDGET


async def test_collect_past_the_page_limit_raises() -> None:
    """Propagate ``PageLimitError`` when the budget is spent before the walk or *max_items* is done."""
    api = _Api(_endless)
    paginated = api.client().paginate("items", expect=decoder(Page), pages=_cursor(), limit=PageLimit(_BUDGET))
    with pytest.raises(PageLimitError):
        await paginated.collect(max_items=_BUDGET + 1)
    assert len(api.sent) == _BUDGET


@pytest.mark.parametrize("max_items", [-1, True], ids=["negative", "a-bool"])
async def test_collect_refuses_a_bad_count(max_items: int) -> None:
    """Refuse a negative count, and a ``bool`` passed as one, before sending anything."""
    api = _Api(_endless)
    paginated = api.client().paginate("items", expect=decoder(Page), pages=_cursor())
    with pytest.raises(ValueError, match=f"^max_items must be None or >= 0 \\(got {max_items!r}\\)$"):
        await paginated.collect(max_items=max_items)
    assert api.sent == []
