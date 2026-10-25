# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""``AsyncBaseClient.paginate``: each strategy, laziness, the full stack per page, and termination."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
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
    PageRequest,
    RequestOptions,
    UpstreamStatusError,
)
from headgate.msgspec import decoder
from headgate.ratelimit import Rate
from tests.support.clients import StubClient

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

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
    pages = Offset(items=items_of, limit=2, start=0)
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
    pages = Offset(items=items_of, limit=1, param="skip", start=10)
    assert [item.id async for item in api.client().paginate("items", expect=decoder(Page), pages=pages)] == [10, 11]
    assert [url.params["skip"] for url in api.sent] == ["10", "11", "12"]


def test_offset_resumes_from_start_when_the_request_lacks_its_parameter() -> None:
    """Treat a request without the offset parameter as being at ``start``."""
    pages = Offset(items=items_of, limit=1, start=3)
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


async def test_a_repeating_cursor_on_a_template_url_raises() -> None:
    """Raise when a cursor repeats on a templated URL, without naming the URL."""

    def answer(request: httpx2.Request) -> httpx2.Response:
        return _page(request, [1], cursor="same")

    pages = Cursor(items=items_of, next=cursor_of, param="after")
    paginated = _Api(answer).client().paginate(t"items/{'x'}", expect=decoder(Page), pages=pages)
    with pytest.raises(ClientError, match="pagination loop") as raised:
        _ = [page async for page in paginated.pages()]
    assert raised.value.url is None


#: One request body of each kind.
_BODIES: list[RequestOptions] = [{"json": {}}, {"data": {}}, {"content": b"x"}, {"files": {"f": b"x"}}]


@pytest.mark.parametrize("body", _BODIES, ids=["json", "data", "content", "files"])
def test_a_body_option_is_refused(body: RequestOptions) -> None:
    """Refuse a request body: every page is a GET."""
    client = _Api(_by_cursor).client()
    with pytest.raises(ValueError, match="no request body"):
        client.paginate("items", expect=decoder(Page), pages=LinkHeader(items=items_of), **body)


@pytest.mark.parametrize(
    "build",
    [
        lambda: Cursor(items=items_of, next=cursor_of, param=""),
        lambda: Offset(items=items_of, limit=0),
        lambda: Offset(items=items_of, limit=1, start=-1),
        lambda: Offset(items=items_of, limit=1, param=""),
    ],
    ids=["empty-cursor-param", "zero-limit", "negative-start", "empty-offset-param"],
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
