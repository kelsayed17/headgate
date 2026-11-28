# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""``Client.paginate``: each strategy, laziness, the full stack per page, and termination."""

from __future__ import annotations

import asyncio
import itertools
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar, Final, assert_type, cast, override

import httpx2
import msgspec
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from headgate import (
    CallPolicy,
    Client,
    ClientError,
    EncodedBody,
    Expect,
    HttpCache,
    HttpOptions,
    Paginated,
    Rate,
    Reader,
    UpstreamStatusError,
)
from headgate._client import _paired
from headgate.errors import ForbiddenDestinationError, PageLimitError, ResponseParseError
from headgate.msgspec import reader, writer
from headgate.pagination import BodyCursor, BodyOffset, Cursor, LinkHeader, NextUrl, Offset, PageNumber, PageRequest
from headgate.testing import FakeTransport

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Iterator, Sequence

    from hishel import AsyncSqliteStorage

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


def has_next(page: Page) -> bool:
    """Return whether *page* names a next page."""
    return page.next is not None


@dataclass
class _Api:
    """An in-memory API: a function of the request answering each page, recording every request it was sent."""

    answer: Callable[[httpx2.Request], httpx2.Response]
    sent: list[httpx2.URL] = field(default_factory=list)

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        """Record *request* and answer it."""
        self.sent.append(request.url)
        return self.answer(request)

    def client(self) -> Client:
        """Return a client over this API."""
        return Client(
            base_url=_BASE, policy=CallPolicy(rate=_RATE), http=HttpOptions(transport=httpx2.MockTransport(self.handle))
        )


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
        pager=Cursor(reader(Page), items=items_of, next=cursor_of, param="after"),
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
        "items", pager=Cursor(reader(Page), items=items_of, next=cursor_of, param="after")
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
    pages = Offset(reader(Page), items=items_of, page_size=2, start=0)
    items = [item.id async for item in api.client().paginate("items", pager=pages, params={"limit": 2})]
    assert items == [0, 1, 2, 3, 4]
    assert [url.params["offset"] for url in api.sent] == ["0", "2", "4"]


async def test_offset_stops_at_an_empty_page_and_counts_from_start() -> None:
    """Stop at an empty page, the first request carrying the configured start."""
    end = 12

    def answer(request: httpx2.Request) -> httpx2.Response:
        skip = int(request.url.params["skip"])
        return _page(request, [skip] if skip < end else [])

    api = _Api(answer)
    pages = Offset(reader(Page), items=items_of, page_size=1, param="skip", start=10)
    assert [item.id async for item in api.client().paginate("items", pager=pages)] == [10, 11]
    assert [url.params["skip"] for url in api.sent] == ["10", "11", "12"]


def test_offset_resumes_from_start_when_the_request_lacks_its_parameter() -> None:
    """Treat a request without the offset parameter as being at ``start``."""
    pages = Offset(reader(Page), items=items_of, page_size=1, start=3)
    following = pages.next_page(Page([Item(1)]), PageRequest("items"), httpx2.Response(200))
    assert following == PageRequest("items", {"offset": 4})


async def test_offset_ends_at_the_total_without_fetching_the_empty_page_past_it() -> None:
    """Stop once the offset reaches the total a page reports, so a last page that is full is the last fetched."""

    def answer(request: httpx2.Request) -> httpx2.Response:
        offset = int(request.url.params["offset"])
        return _page(request, [offset, offset + 1], cursor="4")

    api = _Api(answer)
    pages = Offset(reader(Page), items=items_of, page_size=2, total=lambda page: int(page.cursor or 0))
    assert [item.id async for item in api.client().paginate("items", pager=pages)] == [0, 1, 2, 3]
    assert [url.params["offset"] for url in api.sent] == ["0", "2"]


async def test_offset_without_an_exact_total_ends_at_a_short_page() -> None:
    """Read a total of ``None`` as none stated, leaving the walk to end at a short page."""
    last = 4

    def answer(request: httpx2.Request) -> httpx2.Response:
        offset = int(request.url.params["offset"])
        return _page(request, [offset, offset + 1] if offset < last else [offset])

    api = _Api(answer)
    pages = Offset(reader(Page), items=items_of, page_size=2, total=lambda _: None)
    assert [item.id async for item in api.client().paginate("items", pager=pages)] == [
        0,
        1,
        2,
        3,
        4,
    ]
    assert [url.params["offset"] for url in api.sent] == ["0", "2", "4"]


def _numbered(last: int, *, host: str = _BASE) -> Callable[[httpx2.Request], httpx2.Response]:
    """Serve pages 1 to *last*, one item each (its number), each but the last linking to its successor on *host*."""

    def answer(request: httpx2.Request) -> httpx2.Response:
        number = int(request.url.params["page"])
        following = f"{host}/elsewhere?page={number + 1}" if number < last else None
        return _page(request, [number] if number <= last else [], next=following)

    return answer


async def test_page_numbers_advance_on_the_callers_own_request_while_another_page_follows() -> None:
    """Ask for page 1, 2, 3 on the caller's URL and query, reading the next link only as a yes or no."""
    api = _Api(_numbered(3, host="https://elsewhere.test"))
    pages = PageNumber(reader(Page), items=items_of, more=has_next)
    paginated = api.client().paginate("items", pager=pages, params={"q": "x"})
    items = [item async for item in paginated]
    assert_type(items, list[Item])
    assert [item.id for item in items] == [1, 2, 3]
    assert [str(url) for url in api.sent] == [f"{_BASE}/items?q=x&page={n}" for n in (1, 2, 3)]


@pytest.mark.parametrize("own", [2, "2"], ids=["int", "digits"])
async def test_page_numbers_start_at_the_callers_own_page(own: object) -> None:
    """Start at the page the caller's ``params=`` name, as an int or a string of digits."""
    api = _Api(_numbered(3))
    pages = PageNumber(reader(Page), items=items_of, more=has_next)
    paginated = api.client().paginate("items", pager=pages, params={"page": own})
    assert [item.id async for item in paginated] == [2, 3]
    assert [url.params["page"] for url in api.sent] == ["2", "3"]


async def test_page_numbers_take_their_parameter_and_start() -> None:
    """Send the number under ``param``, from ``start`` when the caller gives none."""

    def answer(request: httpx2.Request) -> httpx2.Response:
        number = int(request.url.params["p"])
        return _page(request, [number], next="more" if number < 1 else None)

    api = _Api(answer)
    pages = PageNumber(reader(Page), items=items_of, more=has_next, param="p", start=0)
    assert [item.id async for item in api.client().paginate("items", pager=pages)] == [0, 1]
    assert [url.params["p"] for url in api.sent] == ["0", "1"]


async def test_an_empty_numbered_page_ends_the_walk_unless_told_otherwise() -> None:
    """End at a page with no items, even when it says another follows; ``stop_on_empty=False`` goes past it."""
    last = 3

    def answer(request: httpx2.Request) -> httpx2.Response:
        number = int(request.url.params["page"])
        return _page(request, [] if number == 1 else [number], next="more" if number < last else None)

    stopping = _Api(answer)
    pages = PageNumber(reader(Page), items=items_of, more=has_next)
    assert [item async for item in stopping.client().paginate("items", pager=pages)] == []
    assert len(stopping.sent) == 1

    going = _Api(answer)
    pages = PageNumber(reader(Page), items=items_of, more=has_next, stop_on_empty=False)
    assert [item.id async for item in going.client().paginate("items", pager=pages)] == [2, 3]


def test_a_numbered_request_without_its_parameter_is_at_start() -> None:
    """Treat a request without the page parameter as being at ``start``."""
    pages = PageNumber(reader(Page), items=items_of, more=has_next, start=5)
    following = pages.next_page(Page([Item(1)], next="more"), PageRequest("items"), httpx2.Response(200))
    assert following == PageRequest("items", {"page": 6})


# ASCII ids: pytest escapes "²" to a node id that mutmut's second in-process collection cannot find.
@pytest.mark.parametrize(
    "own",
    ["two", "-1", "²", -1, True, 2.0],
    ids=["word", "negative-text", "superscript-digit", "negative", "bool", "float"],
)
def test_a_page_number_that_is_not_one_is_refused(own: object) -> None:
    """Refuse a caller's page that is not a whole number >= 0 when the walk is built."""
    client = _Api(_numbered(3)).client()
    with pytest.raises(ValueError, match="page must be a page number"):
        client.paginate("items", pager=PageNumber(reader(Page), items=items_of, more=has_next), params={"page": own})


async def test_link_header_pages_are_followed_as_absolute_urls() -> None:
    """Follow ``rel="next"``, resolved against the answering URL, without re-sending the first page's query."""

    def answer(request: httpx2.Request) -> httpx2.Response:
        page = int(request.url.params.get("page", "1"))
        links = {1: '</items?page=2>; rel="next"', 2: '<https://cdn.test/items?page=3>; rel="next"', 3: ""}
        return httpx2.Response(200, json={"data": [{"id": page}]}, headers={"link": links[page]}, request=request)

    api = _Api(answer)
    paginated = api.client().paginate("items", pager=LinkHeader(reader(Page), items=items_of), params={"q": "x"})
    assert [item.id async for item in paginated] == [1, 2, 3]
    assert [str(url) for url in api.sent] == [
        f"{_BASE}/items?q=x",
        f"{_BASE}/items?page=2",
        "https://cdn.test/items?page=3",
    ]


@pytest.mark.parametrize("kind", ["body", "link"])
async def test_a_next_page_from_https_down_to_http_is_refused_before_it_is_sent(kind: str) -> None:
    """Refuse a next page an ``https`` page names on plain ``http``, as a downgrading redirect is, sending nothing."""
    following = "http://api.test/items?cursor=c"

    def answer(request: httpx2.Request) -> httpx2.Response:
        response = _page(request, [1], next=following)
        response.headers["Link"] = f'<{following}>; rel="next"'
        return response

    pages = (
        NextUrl(reader(Page), items=items_of, next=next_of)
        if kind == "body"
        else LinkHeader(reader(Page), items=items_of)
    )
    api = _Api(answer)
    with pytest.raises(ForbiddenDestinationError, match=r"from https to http://api\.test/items"):
        _ = [item async for item in api.client().paginate("start", pager=pages)]
    assert [str(url) for url in api.sent] == [f"{_BASE}/start"]


async def test_next_url_pages_are_followed_from_the_body() -> None:
    """Follow a next URL the page names, relative or absolute."""

    def answer(request: httpx2.Request) -> httpx2.Response:
        following = {"/a": "b", "/b": f"{_BASE}/c", "/c": None}[request.url.path]
        return _page(request, [len(request.url.path)], next=following)

    api = _Api(answer)
    paginated = api.client().paginate("a", pager=NextUrl(reader(Page), items=items_of, next=next_of))
    assert len([item async for item in paginated]) == len(("a", "b", "c"))
    assert [url.path for url in api.sent] == ["/a", "/b", "/c"]


@pytest.mark.parametrize("kind", ["body", "link"])
async def test_a_next_url_from_the_wire_is_sent_verbatim(kind: str) -> None:
    """Send a next URL read from a body or a ``Link`` header exactly as the server wrote it, escapes included."""
    following = "https://api.test/items/a%2Fb?cursor=a%2Fb&x=1"

    def answer(request: httpx2.Request) -> httpx2.Response:
        if request.url.path != "/start":
            return _page(request, [2])
        if kind == "body":
            return _page(request, [1], next=following)
        response = _page(request, [1])
        response.headers["Link"] = f'<{following}>; rel="next"'
        return response

    pages = (
        NextUrl(reader(Page), items=items_of, next=next_of)
        if kind == "body"
        else LinkHeader(reader(Page), items=items_of)
    )
    api = _Api(answer)
    paginated = api.client().paginate("start", pager=pages)
    assert len([item async for item in paginated]) == len(("start", "items"))
    assert api.sent[1].raw_path == b"/items/a%2Fb?cursor=a%2Fb&x=1"


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
        .paginate("items", pager=Cursor(reader(Page), items=items_of, next=cursor_of, param="after"))
    )

    async def collect(record: Callable[[int], None]) -> None:
        async for item in paginated:
            record(item.id)

    with pytest.raises(UpstreamStatusError) as raised:
        await collect(received.append)
    assert raised.value.status_code == httpx2.codes.NOT_FOUND
    assert received == [1, 2, 3]


async def test_every_page_carries_the_walks_cache_ttl(storage: AsyncSqliteStorage) -> None:
    """Stamp ``cache_ttl=`` on every page's GET, as on one ``get``."""
    ttl = 30.0
    stamped: list[object] = []

    def answer(request: httpx2.Request) -> httpx2.Response:
        stamped.append(request.extensions.get("caching", {}).get("ttl"))
        return _by_cursor(request)

    pages = Cursor(reader(Page), items=items_of, next=cursor_of, param="after")
    client = Client(
        base_url=_BASE,
        policy=CallPolicy(rate=_RATE, cache=HttpCache(storage=storage, ttl=None)),
        http=HttpOptions(transport=httpx2.MockTransport(answer)),
    )
    paginated = client.paginate("items", pager=pages, cache_ttl=ttl)
    assert len([item async for item in paginated]) == len((1, 2, 3, 4))
    assert stamped == [ttl, ttl, ttl]


async def test_a_walk_that_does_not_follow_redirects_raises_a_pages_redirect() -> None:
    """Raise a page's redirect as its 3xx with ``follow_redirects=False``, as on one ``get``."""

    def answer(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/moved":
            return httpx2.Response(302, headers={"Location": f"{_BASE}/items"}, request=request)
        return _by_cursor(request)

    pages = Cursor(reader(Page), items=items_of, next=cursor_of, param="after")
    api = _Api(answer)
    with pytest.raises(UpstreamStatusError) as raised:
        await api.client().paginate("moved", pager=pages, follow_redirects=False).collect()
    assert raised.value.status_code == httpx2.codes.FOUND
    assert [url.path for url in api.sent] == ["/moved"]


async def test_every_page_sends_the_expects_headers_and_accept() -> None:
    """Send an ``Expect``'s ``headers`` and ``accept`` on every page, as one ``get`` does."""
    sent: list[httpx2.Request] = []

    def answer(request: httpx2.Request) -> httpx2.Response:
        sent.append(request)
        page = _by_cursor(request)
        return httpx2.Response(200, json=msgspec.json.decode(page.content), request=request)

    expect = Expect(reader(Page), headers={"X-Api-Version": "2"}, accept="application/json")
    pages = Cursor(expect, items=items_of, next=cursor_of, param="after")
    assert len(await _Api(answer).client().paginate("items", pager=pages).collect()) == len((1, 2, 3, 4))
    assert [(r.headers["X-Api-Version"], r.headers["Accept"]) for r in sent] == [("2", "application/json")] * 3


async def test_a_page_of_another_media_type_than_the_expect_accepts_raises() -> None:
    """Refuse a page whose ``Content-Type`` the ``Expect`` does not accept, before its reader runs."""

    def answer(request: httpx2.Request) -> httpx2.Response:
        page = _by_cursor(request)
        return httpx2.Response(200, content=page.content, headers={"Content-Type": "text/html"}, request=request)

    expect = Expect(reader(Page), accept="application/json")
    paginated = _Api(answer).client().paginate("items", pager=LinkHeader(expect, items=items_of))
    with pytest.raises(ResponseParseError, match="text/html"):
        await paginated.collect()


async def test_a_declared_answer_is_a_page() -> None:
    """Read a status the call declared with ``Expect.on`` as a page."""

    def answer(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(404, request=request)

    expect = Expect(reader(Page)).on(404, Reader(lambda _resp: Page([])))
    paginated = _Api(answer).client().paginate("items", pager=LinkHeader(expect, items=items_of))
    assert [page async for page in paginated.pages()] == [Page([])]


def test_a_paired_reader_keeps_the_inner_readers_name() -> None:
    """Name the reader a pagination ``Expect`` pairs with its response after the reader it wraps."""
    inner = Reader(lambda _resp: Page([]), name="pages")
    assert repr(_paired(inner)) == "Reader(paired Reader(pages))"


async def test_a_page_that_leads_back_raises_instead_of_looping() -> None:
    """Raise ``ClientError`` when a next URL repeats an earlier request."""

    def answer(request: httpx2.Request) -> httpx2.Response:
        return _page(request, [1], next="/items")

    paginated = _Api(answer).client().paginate("items", pager=NextUrl(reader(Page), items=items_of, next=next_of))
    with pytest.raises(ClientError, match="pagination loop") as raised:
        _ = [item async for item in paginated]
    assert raised.value.url == f"{_BASE}/items"
    assert raised.value.source == "api.test"
    assert raised.value.method == "GET"
    assert str(raised.value) == (
        f"[api.test] pagination loop: the page after page 1 repeats an earlier page's request ({_BASE}/items)"
    )


async def test_a_repeating_cursor_on_a_template_url_raises() -> None:
    """Raise when a cursor repeats on a templated URL, without naming the URL."""
    served = itertools.count()

    def answer(request: httpx2.Request) -> httpx2.Response:
        return _page(request, [next(served)], cursor="same")  # pages that differ, so only the request repeats

    pages = Cursor(reader(Page), items=items_of, next=cursor_of, param="after")
    paginated = _Api(answer).client().paginate(t"items/{'x'}", pager=pages)
    with pytest.raises(ClientError, match="pagination loop") as raised:
        _ = [page async for page in paginated.pages()]
    assert raised.value.url is None
    assert str(raised.value) == "[api.test] pagination loop: the page after page 2 repeats an earlier page's request"


#: One request body of each kind no pager can rewrite.
_BODIES: list[dict[str, Any]] = [{"data": {}}, {"content": b"x"}, {"files": {"f": b"x"}}]


@pytest.mark.parametrize("body", _BODIES, ids=["data", "content", "files"])
def test_a_body_no_pager_can_rewrite_is_refused(body: dict[str, Any]) -> None:
    """Refuse a form, raw or multipart body: a walk's body is ``json=``, or ``body=`` with ``write=``."""
    client = _Api(_by_cursor).client()
    name = next(iter(body))
    with pytest.raises(
        TypeError, match=rf"argument '{name}': a walk's body is payload=, the value its pager rewrites for each page$"
    ):
        client.paginate("items", pager=LinkHeader(reader(Page), items=items_of), **body)


@pytest.mark.parametrize(
    "build",
    [
        lambda: Cursor(reader(Page), items=items_of, next=cursor_of, param=""),
        lambda: Offset(reader(Page), items=items_of, page_size=0),
        lambda: Offset(reader(Page), items=items_of, page_size=1, start=-1),
        lambda: Offset(reader(Page), items=items_of, page_size=1, param=""),
        lambda: PageNumber(reader(Page), items=items_of, more=has_next, start=-1),
        lambda: PageNumber(reader(Page), items=items_of, more=has_next, param=""),
    ],
    ids=[
        "empty-cursor-param",
        "zero-page-size",
        "negative-start",
        "empty-offset-param",
        "negative-page-start",
        "empty-page-param",
    ],
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
    client = Client(base_url=_BASE, policy=CallPolicy(rate=_RATE), http=HttpOptions(transport=graph))
    paginated = client.paginate("items", pager=Cursor(reader(Page), items=items_of, next=cursor_of, param="at"))

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
        Cursor(reader(Page), items=items_of, next=cursor_of, param="")


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


def _cursor(*, stop_on_empty: bool = True) -> Pager[Item, Page]:
    """Return a cursor strategy for the linked pages."""
    return Cursor(reader(Page), items=items_of, next=cursor_of, param="at", stop_on_empty=stop_on_empty)


def _next_url(*, stop_on_empty: bool = True) -> Pager[Item, Page]:
    """Return a next-URL strategy for the linked pages."""
    return NextUrl(reader(Page), items=items_of, next=next_of, stop_on_empty=stop_on_empty)


def _link_header(*, stop_on_empty: bool = True) -> Pager[Item, Page]:
    """Return a ``Link`` header strategy for the linked pages."""
    return LinkHeader(reader(Page), items=items_of, stop_on_empty=stop_on_empty)


_LINKED_STRATEGIES = [_cursor, _next_url, _link_header]
_LINKED_IDS = ["cursor", "next-url", "link-header"]


@pytest.mark.parametrize("strategy", _LINKED_STRATEGIES, ids=_LINKED_IDS)
async def test_a_page_with_no_items_ends_the_walk(strategy: Callable[..., Pager[Item, Page]]) -> None:
    """Stop at a page with no items, though it links to another: a link that advances while returning nothing."""
    api = _Api(_with_an_empty_page)
    paginated = api.client().paginate("items", pager=strategy())

    assert [item.id async for item in paginated] == [0]
    assert len(api.sent) == len(("the page with an item", "the empty page"))


@pytest.mark.parametrize("strategy", _LINKED_STRATEGIES, ids=_LINKED_IDS)
async def test_an_empty_page_can_be_walked_past(strategy: Callable[..., Pager[Item, Page]]) -> None:
    """Follow the link past an empty page with ``stop_on_empty=False``, for an endpoint that sends them mid-walk."""
    api = _Api(_with_an_empty_page)
    paginated = api.client().paginate("items", pager=strategy(stop_on_empty=False))

    assert [item.id async for item in paginated] == [0, _LAST]
    assert len(api.sent) == _LAST + 1


async def test_a_walk_past_its_page_limit_raises_with_what_it_fetched() -> None:
    """Raise ``PageLimitError`` once the budget is spent with pages to come, after yielding the pages it allowed."""
    api = _Api(_endless)
    paginated = api.client().paginate("items", pager=_cursor(), max_pages=_BUDGET)
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
        f"[api.test] pagination reached its limit of {_BUDGET} pages with more to come (items); the result is"
        " incomplete. Raise max_pages=, or set on_limit='stop' to end the walk there"
    )


async def test_a_limit_on_a_template_url_raises_without_naming_it() -> None:
    """Raise past the limit on a templated URL, without naming the URL."""
    client = _Api(_endless).client()
    paginated = client.paginate(t"items/{'x'}", pager=_cursor(), max_pages=1)
    with pytest.raises(PageLimitError) as raised:
        _ = [page async for page in paginated.pages()]
    assert (raised.value.method, raised.value.url) == ("GET", None)
    assert str(raised.value) == (
        "[api.test] pagination reached its limit of 1 pages with more to come; the result is incomplete."
        " Raise max_pages=, or set on_limit='stop' to end the walk there"
    )


async def test_a_walk_can_stop_quietly_at_its_page_limit() -> None:
    """End the walk at the budget with ``on_limit="stop"``, for a caller that wants only the first pages."""
    api = _Api(_endless)
    paginated = api.client().paginate("items", pager=_cursor(), max_pages=_BUDGET, on_limit="stop")

    assert [item.id async for item in paginated] == list(range(_BUDGET))
    assert len(api.sent) == _BUDGET


async def test_a_walk_that_ends_exactly_at_its_limit_is_complete() -> None:
    """Raise nothing when the last page is the budget's last: no page was left unfetched."""
    paginated = (
        _Api(_by_cursor)
        .client()
        .paginate(
            "items",
            pager=Cursor(reader(Page), items=items_of, next=cursor_of, param="after"),
            max_pages=3,
        )
    )
    assert [item.id async for item in paginated] == [1, 2, 3, 4]


@pytest.mark.parametrize(
    ("budget", "error", "message"),
    [
        ({"max_pages": 0}, ValueError, "max_pages= takes None or an int of at least 1 (got 0)"),
        ({"max_pages": True}, TypeError, "max_pages= takes None or an int of at least 1 (got bool)"),
        ({"max_pages": 5, "on_limit": "truncate"}, ValueError, "on_limit= takes 'raise' or 'stop' (got 'truncate')"),
        ({"max_pages": 5, "on_limit": True}, TypeError, "on_limit= takes 'raise' or 'stop' (got bool)"),
        ({"on_limit": "stop"}, ValueError, "on_limit='stop' says what happens at max_pages=, so it needs one"),
    ],
    ids=["zero", "a-bool", "unknown-outcome", "an-outcome-not-text", "stop-without-a-budget"],
)
def test_a_bad_page_budget_is_refused_before_anything_is_sent(
    budget: dict[str, Any], error: type[Exception], message: str
) -> None:
    """Refuse a budget below one page, a ``bool`` count, an outcome that does not exist, and a stop with no budget."""
    api = _Api(_endless)
    with pytest.raises(error, match=f"^{re.escape(message)}$"):
        api.client().paginate("items", pager=_cursor(), **budget)
    assert api.sent == []


def test_the_removed_limit_keyword_is_refused() -> None:
    """Refuse ``limit=``, which ``max_pages=`` and ``on_limit=`` replace, as any unknown option is."""
    with pytest.raises(TypeError, match="limit"):
        _Api(_endless).client().paginate("items", pager=_cursor(), **cast("Any", {"limit": 1}))


async def test_collect_returns_every_item_in_order() -> None:
    """Collect the whole walk into a list, typed by the pager's items."""
    api = _Api(_by_cursor)
    paginated = api.client().paginate(
        "items", pager=Cursor(reader(Page), items=items_of, next=cursor_of, param="after")
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
        "items", pager=Cursor(reader(Page), items=items_of, next=cursor_of, param="after")
    )
    assert [item.id for item in await paginated.collect(max_items=max_items)] == ids
    assert len(api.sent) == fetched


async def test_collect_of_no_items_sends_nothing() -> None:
    """Return an empty list for ``max_items=0`` without a request."""
    api = _Api(_endless)
    paginated = api.client().paginate("items", pager=_cursor())
    assert await paginated.collect(max_items=0) == []
    assert api.sent == []


async def test_collect_within_the_page_limit_raises_nothing() -> None:
    """Return the items when they are all collected before the page budget is spent, though more pages follow."""
    api = _Api(_endless)
    paginated = api.client().paginate("items", pager=_cursor(), max_pages=_BUDGET)
    assert [item.id for item in await paginated.collect(max_items=_BUDGET)] == list(range(_BUDGET))
    assert len(api.sent) == _BUDGET


async def test_collect_past_the_page_limit_raises() -> None:
    """Propagate ``PageLimitError`` when the budget is spent before the walk or *max_items* is done."""
    api = _Api(_endless)
    paginated = api.client().paginate("items", pager=_cursor(), max_pages=_BUDGET)
    with pytest.raises(PageLimitError):
        await paginated.collect(max_items=_BUDGET + 1)
    assert len(api.sent) == _BUDGET


@pytest.mark.parametrize(
    ("max_items", "error", "message"),
    [
        (-1, ValueError, r"^max_items must be None or >= 0 \(got -1\)$"),
        (True, TypeError, r"^max_items= takes None or an int of 0 or more \(got bool\)$"),
    ],
    ids=["negative", "a-bool"],
)
async def test_collect_refuses_a_bad_count(max_items: int, error: type[Exception], message: str) -> None:
    """Refuse a negative count, and a ``bool`` passed as one, before sending anything."""
    api = _Api(_endless)
    paginated = api.client().paginate("items", pager=_cursor())
    with pytest.raises(error, match=message):
        await paginated.collect(max_items=max_items)
    assert api.sent == []


#: A page budget that only ends a walk the loop check failed to end.
_BACKSTOP = 10


async def test_an_offset_walk_the_server_does_not_page_raises_at_the_repeated_page() -> None:
    """Raise once a page repeats an earlier one's body, rather than walking the same page forever."""

    def answer(request: httpx2.Request) -> httpx2.Response:
        return _page(request, [0, 1])  # the offset is ignored

    api = _Api(answer)
    pages = Offset(reader(Page), items=items_of, page_size=2)
    with pytest.raises(ClientError, match="pagination loop") as raised:
        _ = [item async for item in api.client().paginate("items", pager=pages, max_pages=_BACKSTOP)]

    assert len(api.sent) == len(("first", "repeated"))
    assert str(raised.value) == (
        f"[api.test] pagination loop: page 2 repeats an earlier page's body, so the server is not paging the walk "
        f"({_BASE}/items?offset=2)"
    )


async def test_a_numbered_walk_the_server_does_not_page_raises_at_the_repeated_page() -> None:
    """Raise at the second page when the server answers every page number with the first page."""

    def answer(request: httpx2.Request) -> httpx2.Response:
        return _page(request, [1], next="more")

    api = _Api(answer)
    pages = PageNumber(reader(Page), items=items_of, more=has_next)
    with pytest.raises(ClientError, match="page 2 repeats an earlier page's body"):
        _ = [item async for item in api.client().paginate("items", pager=pages, max_pages=_BACKSTOP)]

    assert len(api.sent) == len(("first", "repeated"))


async def test_empty_pages_that_repeat_are_walked_past() -> None:
    """Walk past empty pages with the same body when told to, since an empty page proves nothing about paging."""
    last = 4

    def answer(request: httpx2.Request) -> httpx2.Response:
        number = int(request.url.params["page"])
        return _page(request, [number] if number in {1, last} else [], next=None if number == last else "more")

    api = _Api(answer)
    pages = PageNumber(reader(Page), items=items_of, more=has_next, stop_on_empty=False)
    items = [item.id async for item in api.client().paginate("items", pager=pages)]

    assert items == [1, 4]


async def test_link_header_pages_with_the_same_body_are_walked() -> None:
    """Walk ``Link`` pages whose bodies are alike: the next page is read from the response, so a repeat is no loop.

    A sparse series can answer two pages with the same zeros; a server that ignores the page it is asked for repeats
    its ``Link`` too, which the repeated-request check catches.
    """
    last = 3

    def answer(request: httpx2.Request) -> httpx2.Response:
        page = int(request.url.params.get("page", "1"))
        link = f'</items?page={page + 1}>; rel="next"' if page < last else ""
        return httpx2.Response(200, json={"data": [{"id": 0}, {"id": 0}]}, headers={"link": link}, request=request)

    paginated = _Api(answer).client().paginate("items", pager=LinkHeader(reader(Page), items=items_of))

    assert [item.id async for item in paginated] == [0] * 6


@dataclass(frozen=True)
class _OwnOffset:
    """A pager of its own, by its shape: an offset walk that says it counts, whose items come back as an iterator."""

    counts_pages: ClassVar[bool] = True
    read: Reader[Page]
    calls: list[Page] = field(default_factory=list, compare=False)

    def first(self, request: PageRequest[None]) -> PageRequest[None]:
        """Start at offset 0."""
        return request.with_param("offset", 0)

    def page_items(self, page: Page) -> Iterator[Item]:
        """Return *page*'s items as an iterator, which can be read only once, recording the call."""
        self.calls.append(page)
        return iter(page.data)

    def next_page(self, page: Page, request: PageRequest[None], response: httpx2.Response) -> PageRequest[None] | None:
        """Return the request at the offset past *page*, or ``None`` after an empty page."""
        del response
        if not page.data:
            return None
        return request.with_param("offset", int(str((request.params or {})["offset"])) + len(page.data))


@dataclass(frozen=True)
class _UncountedOffset(_OwnOffset):
    """The same walk, without saying it counts: the walk cannot tell its pages from the server's, so it trusts them."""

    counts_pages: ClassVar[bool] = False


async def test_a_counting_pager_of_its_own_gets_the_loop_guard() -> None:
    """Raise at the second page when the server ignores the offset of a pager that says it counts its pages."""
    api = _Api(lambda request: _page(request, [0, 1]))
    paginated = api.client().paginate("items", pager=_OwnOffset(reader(Page)), max_pages=_BACKSTOP)
    with pytest.raises(ClientError, match="page 2 repeats an earlier page's body"):
        _ = [item async for item in paginated]

    assert len(api.sent) == len(("first", "repeated"))


async def test_a_pager_that_does_not_say_it_counts_is_not_body_checked() -> None:
    """Walk to the page budget a pager that does not declare ``counts_pages``, as the server answers it."""
    api = _Api(lambda request: _page(request, [0, 1]))
    paginated = api.client().paginate("items", pager=_UncountedOffset(reader(Page)), max_pages=3, on_limit="stop")

    assert len([item async for item in paginated]) == len((0, 1)) * 3


async def test_a_pager_whose_items_are_one_iterator_yields_them_all_from_one_call() -> None:
    """Read a page's items once, though the loop guard looked at the first: every item comes through."""
    end = 4

    def answer(request: httpx2.Request) -> httpx2.Response:
        offset = int(request.url.params["offset"])
        return _page(request, [offset, offset + 1] if offset < end else [])

    pager = _OwnOffset(reader(Page))
    paginated = _Api(answer).client().paginate("items", pager=pager)

    assert [item.id async for item in paginated] == [0, 1, 2, 3]
    assert len(pager.calls) == len(("0-1", "2-3", "empty")), "page_items is called once per page"


@dataclass(frozen=True)
class _SharedItems(_OwnOffset):
    """A counting pager whose ``page_items`` hands out one iterator per page, however often it is asked."""

    shared: dict[int, Iterator[Item]] = field(default_factory=dict, compare=False)

    @override
    def page_items(self, page: Page) -> Iterator[Item]:
        """Return the one iterator over *page*'s items."""
        return self.shared.setdefault(id(page), iter(page.data))


async def test_a_page_whose_items_are_one_shared_iterator_loses_none_to_the_loop_guard() -> None:
    """Yield the first item too, which the loop guard reads to see whether the page holds any."""
    api = _Api(lambda request: _page(request, [1, 2, 3] if request.url.params["offset"] == "0" else []))
    paginated = api.client().paginate("items", pager=_SharedItems(reader(Page)))

    assert [item.id async for item in paginated] == [1, 2, 3]


def test_the_counting_strategies_say_they_count() -> None:
    """Declare ``counts_pages`` on the strategies that compute the next request, and only on them."""
    counting = {Offset, PageNumber, BodyOffset}
    for strategy in (Cursor, Offset, PageNumber, LinkHeader, NextUrl, BodyCursor, BodyOffset):
        assert getattr(strategy, "counts_pages", False) is (strategy in counting), strategy


async def test_an_offset_walk_starts_at_the_callers_own_offset() -> None:
    """Start at the offset the caller's ``params=`` give, as a numbered walk starts at the caller's page."""
    end = 22

    def answer(request: httpx2.Request) -> httpx2.Response:
        offset = int(request.url.params["offset"])
        return _page(request, [offset] if offset < end else [])

    api = _Api(answer)
    pages = Offset(reader(Page), items=items_of, page_size=1)
    paginated = api.client().paginate("items", pager=pages, params={"offset": "20"})

    assert [item.id async for item in paginated] == [20, 21]
    assert [url.params["offset"] for url in api.sent] == ["20", "21", "22"]


def test_an_offset_that_is_not_one_is_refused() -> None:
    """Refuse a caller's offset that is no offset when the walk is built."""
    with pytest.raises(ValueError, match="offset must be an offset"):
        Offset(reader(Page), items=items_of, page_size=1).first(PageRequest("items", {"offset": "x"}))


@pytest.mark.parametrize("served", [2, 1], ids=["full-pages", "capped-pages"])
async def test_a_one_based_offset_walk_reaches_its_total(served: int) -> None:
    """Fetch every item of a 1-based walk up to its total, even when the server caps pages below ``page_size``."""
    total = 3

    def answer(request: httpx2.Request) -> httpx2.Response:
        offset = int(request.url.params["offset"])
        return _page(request, [i for i in range(offset, offset + served) if i <= total], cursor=str(total))

    api = _Api(answer)
    pages = Offset(reader(Page), items=items_of, page_size=2, start=1, total=lambda page: int(page.cursor or 0))

    assert [item.id async for item in api.client().paginate("items", pager=pages)] == [1, 2, 3]


async def test_a_next_page_url_that_does_not_parse_is_a_client_error() -> None:
    """Raise ``ClientError`` for a next URL httpx2 cannot parse, rather than its bare ``InvalidURL``."""

    def answer(request: httpx2.Request) -> httpx2.Response:
        return _page(request, [1], next="http://[::1")

    paginated = _Api(answer).client().paginate("items", pager=NextUrl(reader(Page), items=items_of, next=next_of))
    with pytest.raises(ClientError, match="invalid next-page URL after page 1") as raised:
        _ = [item async for item in paginated]

    assert type(raised.value) is ClientError
    assert raised.value.source == "api.test"


async def test_a_link_anchored_to_another_resource_ends_the_walk() -> None:
    """Stop at a page whose only ``rel="next"`` is anchored to another resource: it names no page after this one."""

    def answer(request: httpx2.Request) -> httpx2.Response:
        link = '</items?page=2>; rel="next"; anchor="/elsewhere"'
        return httpx2.Response(200, json={"data": [{"id": 1}]}, headers={"link": link}, request=request)

    api = _Api(answer)
    paginated = api.client().paginate("items", pager=LinkHeader(reader(Page), items=items_of))

    assert [item.id async for item in paginated] == [1]
    assert len(api.sent) == 1


def _soda(total: int) -> Callable[[httpx2.Request], httpx2.Response]:
    """Serve *total* items by ``$offset`` and ``$limit``, as a SODA endpoint pages them."""

    def answer(request: httpx2.Request) -> httpx2.Response:
        offset, limit = int(request.url.params["$offset"]), int(request.url.params["$limit"])
        return _page(request, list(range(offset, min(offset + limit, total))))

    return answer


#: A SODA walk's pager: the offset in ``$offset``, the page size in ``$limit``.
_SODA_PAGES = Offset(reader(Page), items=items_of, page_size=2, param="$offset", size_param="$limit")


async def test_an_offset_walk_steps_by_the_page_size_the_caller_sends() -> None:
    """Step by the caller's own ``$limit``, send it with every page, and end at the page shorter than it."""
    api = _Api(_soda(total=8))
    paginated = api.client().paginate("rows", pager=_SODA_PAGES, params={"$limit": "3"})

    assert [item.id async for item in paginated] == list(range(8))
    assert [(url.params["$offset"], url.params["$limit"]) for url in api.sent] == [("0", "3"), ("3", "3"), ("6", "3")]


async def test_an_offset_walk_sends_its_page_size_when_the_caller_sends_none() -> None:
    """Send ``page_size`` under ``size_param`` when the caller's ``params=`` name no size."""
    api = _Api(_soda(total=3))
    paginated = api.client().paginate("rows", pager=_SODA_PAGES)

    assert [item.id async for item in paginated] == [0, 1, 2]
    assert [(url.params["$offset"], url.params["$limit"]) for url in api.sent] == [("0", "2"), ("2", "2")]


@pytest.mark.parametrize("size", ["x", "0", 0, -1, True])
def test_a_page_size_that_is_not_one_is_refused(size: object) -> None:
    """Refuse a caller's page size that is not an int of at least 1, or a string of digits, when the walk is built."""
    with pytest.raises(ValueError, match=r"\$limit must be a page size"):
        _SODA_PAGES.first(PageRequest("rows", {"$limit": size}))


@pytest.mark.parametrize(
    ("size_param", "message"),
    [("", "size_param must name a query parameter"), ("$offset", "size_param must differ from param")],
)
def test_an_offset_refuses_a_size_param_it_cannot_send(size_param: str, message: str) -> None:
    """Refuse an empty size parameter, and one that names the offset's own parameter."""
    with pytest.raises(ValueError, match=re.escape(message)):
        Offset(reader(Page), items=items_of, page_size=2, param="$offset", size_param=size_param)


class _NoReader:
    """Just enough of a pager to fail when asked for its first page, and no ``read``."""

    def first(self, request: PageRequest[None]) -> PageRequest[None]:
        """Fail: ``paginate`` must check the pager's reader before it runs the pager."""
        del request
        msg = "the pager ran before its read was checked"
        raise AssertionError(msg)

    def page_items(self, page: object) -> list[object]:
        """Never called, as :meth:`first` is not."""
        raise AssertionError(page)

    def next_page(self, page: object, request: object, response: object) -> None:
        """Never called, as :meth:`first` is not."""
        raise AssertionError(page, request, response)


@dataclass(frozen=True)
class _WrongReader(_NoReader):
    """A :class:`_NoReader` whose ``read`` is not a reader."""

    read: object


@pytest.mark.parametrize(
    ("pager", "got"),
    [
        (_NoReader(), "the _NoReader has no read"),
        (_WrongReader(Page), "the _WrongReader has a read of the class Page"),
        (_WrongReader(items_of), "the _WrongReader has a read of function"),
    ],
    ids=["none", "class", "function"],
)
async def test_a_pager_without_a_reader_is_refused(pager: object, got: str) -> None:
    """Refuse a pager whose ``read`` is missing or not a reader, naming it, before the pager runs or sends anything."""
    fake = FakeTransport()
    async with Client(base_url=_BASE, http=HttpOptions(transport=fake)) as client:
        with pytest.raises(TypeError) as caught:
            client.paginate("x", pager=cast("Any", pager))
    assert str(caught.value) == (
        f"paginate()'s pager reads each page with its read, a Reader or an Expect ({got}): pass the page reader "
        "first, as in Cursor(reader(Page), items=..., next=..., param=...)"
    )
    assert not fake.calls


@pytest.mark.parametrize(
    ("pager", "options"),
    [
        (Cursor, {"items": items_of, "next": cursor_of, "param": "after"}),
        (Offset, {"items": items_of, "page_size": 1}),
        (PageNumber, {"items": items_of, "more": has_next}),
        (LinkHeader, {"items": items_of}),
        (NextUrl, {"items": items_of, "next": next_of}),
        (BodyCursor, {"items": items_of, "next": cursor_of, "put": lambda body, _cursor: body}),
        (BodyOffset, {"items": items_of, "page_size": 1, "offset": lambda _body: 0, "put": lambda body, _at: body}),
    ],
    ids=["cursor", "offset", "page-number", "link-header", "next-url", "body-cursor", "body-offset"],
)
def test_a_pager_cannot_be_built_without_its_reader(pager: Callable[..., object], options: dict[str, Any]) -> None:
    """Refuse a pager built without its reader, rather than default ``read`` to something that reads nothing."""
    with pytest.raises(TypeError, match="missing 1 required positional argument: 'read'"):
        pager(**options)


async def test_paginate_returns_a_paginated() -> None:
    """Return a ``Paginated``, the final class the walk is, whose name a misuse's message shows."""
    paginated = _Api(_by_cursor).client().paginate("items", pager=_cursor())

    assert type(paginated) is Paginated
    assert getattr(Paginated, "__final__", False)
    with pytest.raises(TypeError, match=r"^'Paginated' object can't be awaited$"):
        await cast("Any", paginated)
    with pytest.raises(TypeError, match=r"^'Paginated' object is not iterable$"):
        iter(cast("Any", paginated))


async def test_a_test_double_builds_a_paginated_from_its_pages() -> None:
    """Build a ``Paginated`` from what starts its pages and what lists a page's items, as a fake SDK does."""

    async def pages() -> AsyncIterator[list[int]]:
        yield [1, 2]
        yield [3]

    walk: Paginated[int] = Paginated(pages, lambda page: page)

    assert [item async for item in walk] == [1, 2, 3]
    assert [page async for page in walk.pages()] == [[1, 2], [3]]
    assert await walk.collect(max_items=2) == [1, 2]
    assert await walk.collect() == [1, 2, 3], "each iteration starts the pages again"


class _CountedItems:
    """An ``items=`` hook that counts its calls."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, page: Page) -> list[Item]:
        self.calls += 1
        return page.data


def _three_pages(request: httpx2.Request) -> httpx2.Response:
    """Serve two full pages of two items and a short third, by offset, page number, link or next URL."""
    params = request.url.params
    index = int(params["offset"]) // 2 if "offset" in params else int(params.get("page", "1")) - 1
    ids = [[1, 2], [3, 4], [5]][index]
    following = f"{_BASE}/items?page={index + 2}" if index < len(("1-2", "3-4")) else None
    response = _page(request, ids, next=following)
    if following is not None:
        response.headers["Link"] = f'<{following}>; rel="next"'
    return response


#: Five items in three pages, linked by the cursors ``b`` and ``c``.
_FIVE_BY_CURSOR: Final = {None: ([1, 2], "b"), "b": ([3, 4], "c"), "c": ([5], None)}


def _five_by_cursor(request: httpx2.Request) -> httpx2.Response:
    """Serve five items in three pages, linked by the cursor sent as the query parameter ``after``."""
    ids, following = _FIVE_BY_CURSOR[request.url.params.get("after")]
    return _page(request, ids, cursor=following)


def _by_body(request: httpx2.Request) -> httpx2.Response:
    """Serve the pages a walk of POSTs asks for in its body: by ``offset``, two items a page, or by the cursor."""
    body = msgspec.json.decode(request.content or b"{}")
    if "offset" in body:
        return _page(request, [[1, 2], [3, 4], [5]][body["offset"] // 2])
    ids, following = _FIVE_BY_CURSOR[body.get("after")]
    return _page(request, ids, cursor=following)


#: Each strategy as a walk of the same five items: its class, its options but ``read`` and ``items``, the API that
#: answers it, and the walk's ``payload=``.
_STRATEGIES: dict[str, tuple[type[Any], dict[str, Any], Callable[[httpx2.Request], httpx2.Response], object]] = {
    "offset": (Offset, {"page_size": 2}, _three_pages, None),
    "page-number": (PageNumber, {"more": has_next}, _three_pages, None),
    "cursor": (Cursor, {"next": cursor_of, "param": "after"}, _five_by_cursor, None),
    "link": (LinkHeader, {}, _three_pages, None),
    "next-url": (NextUrl, {"next": next_of}, _three_pages, None),
    "body-cursor": (BodyCursor, {"next": cursor_of, "put": lambda body, after: {**body, "after": after}}, _by_body, {}),
    "body-offset": (
        BodyOffset,
        {"page_size": 2, "offset": lambda body: body.get("offset", 0), "put": lambda body, at: {**body, "offset": at}},
        _by_body,
        {},
    ),
}


def _walk(strategy: str, cls: type[Any], items: Callable[[Page], Sequence[Item]]) -> tuple[_Api, Any]:
    """Return the API that answers *strategy* and a walk of it by a pager of *cls* reading items with *items*."""
    _, options, answer, payload = _STRATEGIES[strategy]
    api = _Api(answer)
    return api, api.client().paginate("items", pager=cls(reader(Page), items=items, **options), payload=payload)


@pytest.mark.parametrize("subclassed", [False, True], ids=["itself", "subclass"])
@pytest.mark.parametrize("strategy", sorted(_STRATEGIES))
async def test_a_strategy_calls_its_items_hook_once_per_page(strategy: str, *, subclassed: bool) -> None:
    """Read a page's items once, for the loop guard, the next request and the items yielded alike.

    A subclass that only adds to the strategy, overriding neither ``next_page`` nor ``page_items``, is walked as the
    strategy is.
    """
    base = _STRATEGIES[strategy][0]
    items = _CountedItems()
    api, walk = _walk(strategy, type(f"My{base.__name__}", (base,), {}) if subclassed else base, items)
    walked = [item.id async for item in walk]

    assert walked == [1, 2, 3, 4, 5]
    assert items.calls == len(api.sent), "each page's items are read once"


async def test_a_strategys_items_that_returns_text_is_refused() -> None:
    """Refuse ``items=`` returning a ``str``, a sequence of characters the walk would yield one by one as items."""
    api = _Api(_three_pages)
    pager = Offset(reader(Page), items=cast("Any", lambda _page: "abc"), page_size=2)
    with pytest.raises(TypeError, match=r"^items= returns the items on a page, a collection, such as a list, not text"):
        await api.client().paginate("items", pager=pager).collect()


@pytest.mark.parametrize("strategy", ["body-cursor", "body-offset"])
async def test_a_writing_pager_encodes_each_page_once(strategy: str) -> None:
    """Call a writer once a page: the bytes it wrote are both what the loop guard remembers and what is sent."""
    base, options, answer, payload = _STRATEGIES[strategy]
    encode = writer(dict[str, object])
    calls: list[object] = []

    def write(body: dict[str, object]) -> EncodedBody:
        calls.append(body)
        return encode(body)

    api = _Api(answer)
    pager = base(reader(Page), items=items_of, write=write, **options)
    walked = [item.id async for item in api.client().paginate("items", pager=pager, payload=payload)]

    assert walked == [1, 2, 3, 4, 5]
    assert len(calls) == len(api.sent), "each page's body is written once"


@pytest.mark.parametrize("strategy", sorted(_STRATEGIES))
async def test_a_strategy_subclass_that_overrides_next_page_is_walked_by_it(strategy: str) -> None:
    """Walk by a subclass's own ``next_page``, never the strategy's: here, one that ends the walk at its first page."""
    base = _STRATEGIES[strategy][0]

    class FirstPageOnly(base):
        @override
        def next_page(self, page: Page, request: Any, response: httpx2.Response) -> None:
            del page, request, response

    api, walk = _walk(strategy, FirstPageOnly, items_of)
    walked = [item.id async for item in walk]

    assert walked == [1, 2], "the subclass's next_page ended the walk after the first page"
    assert len(api.sent) == 1


async def test_a_strategy_subclass_that_filters_page_items_is_still_counted_by_the_strategy() -> None:
    """Step an offset by the items the strategy reads, not by the ones a subclass's ``page_items`` keeps."""

    class OddItems(Offset[Item, Page, None]):
        @override
        def page_items(self, page: Page) -> list[Item]:
            return [item for item in page.data if item.id % 2]

    api, walk = _walk("offset", OddItems, items_of)
    walked = [item.id async for item in walk]

    assert walked == [1, 3, 5]
    assert [url.params["offset"] for url in api.sent] == ["0", "2", "4"]


async def test_a_custom_pager_is_walked_by_its_next_page_whatever_else_it_defines() -> None:
    """Walk a pager of the caller's own by ``next_page``, though it has a method named as a strategy's private one."""

    @dataclass(frozen=True)
    class OnePage:
        read: Reader[Page] = field(default_factory=lambda: reader(Page))

        def first(self, request: PageRequest) -> PageRequest:
            return request

        def page_items(self, page: Page) -> list[Item]:
            return page.data

        def next_page(self, page: Page, request: PageRequest, response: httpx2.Response) -> None:
            del page, request, response

        def _next_after(self, *args: object) -> PageRequest:
            """Stand in for a strategy's private hook, which the walk must not take for one."""
            return PageRequest("never-called", params={"args": len(args)})

    api = _Api(_three_pages)
    walked = [item.id async for item in api.client().paginate("items", pager=OnePage())]

    assert walked == [1, 2]


def test_a_strategy_called_directly_reads_its_page_itself() -> None:
    """Answer ``next_page`` called outside a walk, reading the page's items with its own hook."""
    page = Page([Item(1)], cursor="c", next=f"{_BASE}/items?page=2")
    response = httpx2.Response(
        200, headers={"Link": f'<{_BASE}/items?page=2>; rel="next"'}, request=httpx2.Request("GET", f"{_BASE}/items")
    )
    request = PageRequest[None]("items")
    query: dict[str, object] = {"q": 1}
    posted = PageRequest[dict[str, object]]("items", payload=query)

    cursor = Cursor(reader(Page), items=items_of, next=cursor_of, param="after").next_page(page, request, response)
    link = LinkHeader(reader(Page), items=items_of).next_page(page, request, response)
    next_url = NextUrl(reader(Page), items=items_of, next=next_of).next_page(page, request, response)
    body = BodyCursor[Item, Page, dict[str, object]](
        reader(Page), items=items_of, next=cursor_of, put=lambda query, after: {**query, "after": after}
    ).next_page(page, posted, response)

    assert cursor is not None
    assert cursor.params == {"after": "c"}
    assert link is not None
    assert str(link.url) == f"{_BASE}/items?page=2"
    assert next_url is not None
    assert str(next_url.url) == f"{_BASE}/items?page=2"
    assert body is not None
    assert body.payload == {"q": 1, "after": "c"}


def _one_page(request: httpx2.Request) -> httpx2.Response:
    """Answer every page with two items, a cursor and a next URL, which the hooks below are handed."""
    return _page(request, [1, 2], cursor="b", next="https://api.test/next")


def _returning(value: object) -> Callable[..., Any]:
    """Return a hook returning *value*, whatever it is handed: a hook of the wrong kind, typed as any hook."""

    def hook(*args: object) -> object:
        del args
        return value

    return hook


def _with_cursor(query: dict[str, object], cursor: str) -> dict[str, object]:
    """Return *query* with *cursor* in it."""
    return {**query, "cursor": cursor}


#: Each pager with a hook returning the wrong kind, the payload its walk takes, and the refusal naming the hook.
_WRONG_RETURNS: Final[list[tuple[Pager[Any, Page, Any], dict[str, object] | None, str]]] = [
    (
        Cursor(reader(Page), items=_returning(None), next=cursor_of, param="after"),
        None,
        r"^items= returns the items on a page, a collection, such as a list \(got NoneType\)$",
    ),
    (
        Offset(reader(Page), items=_returning(item for item in ()), page_size=2),
        None,
        r"^items= returns the items on a page, a collection, such as a list \(got generator\)$",
    ),
    (
        Offset(reader(Page), items=items_of, page_size=2, total=_returning("10")),
        None,
        r"^total= returns the collection's exact size, an int, or None \(got str\)$",
    ),
    (
        Offset(reader(Page), items=items_of, page_size=2, total=_returning(value=True)),
        None,
        r"^total= returns the collection's exact size, an int, or None \(got bool\)$",
    ),
    (
        Cursor(reader(Page), items=items_of, next=_returning(5), param="after"),
        None,
        r"^next= returns the next page's cursor, a str, or None \(got int\)$",
    ),
    (
        BodyCursor(reader(Page), items=items_of, next=_returning(5), put=_with_cursor),
        {"q": "x"},
        r"^next= returns the next page's cursor, a str, or None \(got int\)$",
    ),
    (
        NextUrl(reader(Page), items=items_of, next=_returning(5)),
        None,
        r"^next= returns the next page's URL, a str or an httpx2\.URL, or None \(got int\)$",
    ),
    (
        Cursor(reader(Page), items=items_of, next=cursor_of, param="after", write=_returning(b"{}")),
        {"q": "x"},
        r"^write= returns an EncodedBody, as a flavor's writer\(Type\) builds one \(got bytes\)$",
    ),
]


@pytest.mark.parametrize(
    ("pager", "payload", "message"),
    _WRONG_RETURNS,
    ids=["items", "items-generator", "total", "total-bool", "next-cursor", "next-body-cursor", "next-url", "write"],
)
async def test_a_hook_that_returns_the_wrong_kind_is_refused_naming_it(
    pager: Pager[Any, Page, Any], payload: dict[str, object] | None, message: str
) -> None:
    """Refuse what a hook returned when it is of the wrong kind, naming the hook, as a reader's ``refuse=`` is."""
    paginated = _Api(_one_page).client().paginate("items", payload=payload, pager=pager)

    with pytest.raises(TypeError, match=message):
        await paginated.collect()


class _ItemsOfNothing:
    """A pager of its own whose ``page_items`` returns no iterable."""

    read: Final = reader(Page)

    def first(self, request: PageRequest) -> PageRequest:
        """Return *request* unchanged."""
        return request

    def page_items(self, page: Page) -> None:
        """Return ``None``, where a page's items go."""
        del page

    def next_page(self, page: Page, request: PageRequest, response: httpx2.Response) -> PageRequest | None:
        """Return ``None``: one page."""
        del page, request, response


async def test_a_pagers_own_page_items_returning_no_iterable_is_refused_naming_it() -> None:
    """Name a pager's own ``page_items`` when it returns what is not an iterable, as ``items=`` is named."""
    paginated = _Api(_one_page).client().paginate("items", pager=cast("Any", _ItemsOfNothing()))

    with pytest.raises(TypeError, match=r"^_ItemsOfNothing\.page_items\(\) returns the items on a page, an iterable"):
        await paginated.collect()


async def test_a_strategy_subclasss_own_page_items_is_named_for_what_it_returns() -> None:
    """Name a subclass's ``page_items``, not the ``items=`` it overrides, when it returns no iterable."""

    class NoItems(Offset[Item, Page, None]):
        @override
        def page_items(self, page: Page) -> Any:
            del page

    _, walk = _walk("offset", NoItems, items_of)

    with pytest.raises(
        TypeError, match=r"^NoItems\.page_items\(\) returns the items on a page, an iterable \(got NoneType\)$"
    ):
        await walk.collect()


def test_a_body_offset_whose_offset_returns_no_int_is_refused_naming_it() -> None:
    """Refuse an ``offset`` that returns what is not an ``int``, which the walk compares and adds to, naming it."""
    pager = BodyOffset(reader(Page), items=items_of, page_size=2, offset=_returning("0"), put=_returning({}))

    with pytest.raises(TypeError, match=r"^offset= returns a body's offset, an int \(got str\)$"):
        _Api(_one_page).client().paginate("items", payload={"offset": 0}, pager=pager)


def test_a_class_given_as_write_is_refused_where_the_pager_is_built() -> None:
    """Refuse a class as ``write=``, which is called with the payload and returns no ``EncodedBody``, as ``Reader``."""
    message = (
        r"^write= takes a writer, such as a flavor's writer\(Query\), or None to send builtins as JSON \(got the class "
        r"dict\): write writer\(dict\) with a flavor's writer, or leave write= out to send builtins as JSON$"
    )
    with pytest.raises(TypeError, match=message):
        Cursor(reader(Page), items=items_of, next=cursor_of, param="after", write=cast("Any", dict))
