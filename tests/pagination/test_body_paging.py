# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""Walks of POSTs: ``BodyCursor`` and ``BodyOffset`` page through the request body, and every page is a ``post``.

An in-memory API reads each page's JSON body and answers from it, so what a walk rewrote shows in what was sent.
"""

from __future__ import annotations

import io
import json
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Final, assert_type, override

import httpx2
import msgspec
import pytest

from headgate import (
    BodyCursor,
    BodyOffset,
    ClientError,
    Cursor,
    Expect,
    HttpOptions,
    IdempotencyKeys,
    LinkHeader,
    NextUrl,
    Offset,
    PageLimit,
    PageLimitError,
    PageOptions,
    RetryPolicy,
    TransportPolicy,
    UpstreamStatusError,
    Writer,
)
from headgate.msgspec import decoder, encoder
from headgate.pagination import PageRequest, WritingPager
from headgate.ratelimit import Rate
from headgate.retry import IDEMPOTENT_METHODS
from tests.support.clients import StubClient

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from headgate.pagination import Pager

_BASE: Final = "https://api.test"
_RATE: Final = Rate.per_second(1000)


class Item(msgspec.Struct, frozen=True):
    """One item on a page."""

    id: int


class Page(msgspec.Struct, frozen=True):
    """A page of items, naming the next page by cursor."""

    data: list[Item]
    cursor: str | None = None


class ToolParams(msgspec.Struct, frozen=True):
    """A JSON-RPC ``tools/call``'s parameters: the tool, and its arguments."""

    name: str
    arguments: dict[str, str]


class ToolCall(msgspec.Struct, frozen=True):
    """A JSON-RPC ``tools/call``, as the tool-call walk sends it."""

    method: str
    params: ToolParams


class Query(msgspec.Struct, frozen=True):
    """A query body: a filter, a window and where in the results it starts."""

    kind: str
    limit: int
    offset: int = 0
    cursor: str | None = None


QUERY: Final = encoder(Query)


def items_of(page: Page) -> list[Item]:
    """Return the items on *page*."""
    return page.data


def cursor_of(page: Page) -> str | None:
    """Return *page*'s cursor."""
    return page.cursor


def with_cursor(query: Query, cursor: str) -> Query:
    """Return *query* carrying *cursor*."""
    return msgspec.structs.replace(query, cursor=cursor)


def offset_of(query: Query) -> int:
    """Return *query*'s offset."""
    return query.offset


def at_offset(query: Query, offset: int) -> Query:
    """Return *query* at *offset*."""
    return msgspec.structs.replace(query, offset=offset)


#: The cursor walk over :class:`Query` bodies.
QUERY_CURSOR: Final = BodyCursor[Page, Item, Query](items=items_of, next=cursor_of, put=with_cursor, write=QUERY)
#: The offset walk over :class:`Query` bodies, two items a page.
QUERY_OFFSET: Final = BodyOffset[Page, Item, Query](
    items=items_of, page_size=2, offset=offset_of, put=at_offset, write=QUERY
)


@dataclass
class _Api:
    """An in-memory API: a function of the request answering each page, recording every request it was sent."""

    answer: Callable[[httpx2.Request], httpx2.Response]
    policy: TransportPolicy = field(default_factory=lambda: TransportPolicy(breaker=None))
    sent: list[httpx2.Request] = field(default_factory=list)

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        """Record *request* and answer it."""
        self.sent.append(request)
        return self.answer(request)

    def client(self) -> StubClient:
        """Return a client over this API."""
        transport = httpx2.MockTransport(self.handle)
        return StubClient(base_url=_BASE, rate=_RATE, policy=self.policy, http=HttpOptions(transport=transport))

    def bodies[T](self, type_: type[T]) -> list[T]:
        """Return the body of every request sent, decoded as *type_*."""
        return [msgspec.json.decode(request.content, type=type_) for request in self.sent]


def _page(request: httpx2.Request, ids: Sequence[int], cursor: str | None = None) -> httpx2.Response:
    """Answer *request* with a page of *ids* naming *cursor* next."""
    body = msgspec.json.encode(Page([Item(i) for i in ids], cursor))
    return httpx2.Response(200, content=body, request=request)


#: The pages of the cursor walk: the cursor a body carries, to the items and cursor of its page.
_CURSOR_PAGES: Final = {None: ([1, 2], "b"), "b": ([3], "c"), "c": ([4], None)}


def _by_body_cursor(request: httpx2.Request) -> httpx2.Response:
    """Serve three pages linked by cursors ``b`` and ``c``, read from the body's ``cursor``."""
    ids, following = _CURSOR_PAGES[json.loads(request.content).get("cursor")]
    return _page(request, ids, following)


def _by_body_offset(total: int) -> Callable[[httpx2.Request], httpx2.Response]:
    """Serve *total* items, a ``limit`` at a time from the body's ``offset``."""

    def answer(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        start = body["offset"]
        return _page(request, [i for i in range(start, start + body["limit"]) if i < total])

    return answer


def _endless(request: httpx2.Request) -> httpx2.Response:
    """Answer every page with one item and a cursor to a new page, so the walk never ends on its own."""
    at = int(json.loads(request.content).get("cursor") or "0")
    return _page(request, [at], str(at + 1))


async def test_a_body_cursor_walk_posts_each_cursor_in_the_body() -> None:
    """POST the caller's body, then the same body carrying each cursor, written by the request's writer."""
    api = _Api(_by_body_cursor)
    first = Query(kind="a", limit=2)
    paginated = api.client().paginate("search", body=first, params={"v": "1"}, expect=decoder(Page), pages=QUERY_CURSOR)

    items = [item async for item in paginated]

    assert_type(items, list[Item])
    assert [item.id for item in items] == [1, 2, 3, 4]
    assert {request.method for request in api.sent} == {"POST"}
    assert {str(request.url) for request in api.sent} == {f"{_BASE}/search?v=1"}
    assert {request.headers["content-type"] for request in api.sent} == {"application/json"}
    assert api.bodies(dict[str, object]) == [
        {"kind": "a", "limit": 2, "offset": 0, "cursor": None},
        {"kind": "a", "limit": 2, "offset": 0, "cursor": "b"},
        {"kind": "a", "limit": 2, "offset": 0, "cursor": "c"},
    ]
    assert first == Query(kind="a", limit=2), "the caller's body is left as it was"


def _put_tool_cursor(call: dict[str, object], cursor: str) -> dict[str, object]:
    """Return a JSON-RPC ``tools/call`` envelope with *cursor* among its arguments, leaving *call* as it was."""
    params = call["params"]
    assert isinstance(params, dict)
    return {**call, "params": {**params, "arguments": {**params["arguments"], "cursor": cursor}}}


async def test_a_body_without_a_writer_is_sent_as_json() -> None:
    """Send a body of builtins as ``json=``, here a JSON-RPC call whose cursor is one of its arguments."""

    def answer(request: httpx2.Request) -> httpx2.Response:
        arguments = json.loads(request.content)["params"]["arguments"]
        ids, following = _CURSOR_PAGES[arguments.get("cursor")]
        return _page(request, ids, following)

    api = _Api(answer)
    call: dict[str, object] = {"method": "tools/call", "params": {"name": "orders", "arguments": {"state": "open"}}}
    pages = BodyCursor[Page, Item, dict[str, object]](items=items_of, next=cursor_of, put=_put_tool_cursor)
    paginated = api.client().paginate("mcp", json=call, expect=decoder(Page), pages=pages)

    assert [item.id async for item in paginated] == [1, 2, 3, 4]
    assert [body.params.arguments for body in api.bodies(ToolCall)] == [
        {"state": "open"},
        {"state": "open", "cursor": "b"},
        {"state": "open", "cursor": "c"},
    ]
    assert call == {"method": "tools/call", "params": {"name": "orders", "arguments": {"state": "open"}}}


async def test_a_query_cursor_walk_can_post_a_body() -> None:
    """Page a POST by a query-parameter cursor, sending the same body with every page."""

    def answer(request: httpx2.Request) -> httpx2.Response:
        ids, following = _CURSOR_PAGES[request.url.params.get("after")]
        return _page(request, ids, following)

    api = _Api(answer)
    pages = Cursor[Page, Item, Query](items=items_of, next=cursor_of, param="after", write=QUERY)
    paginated = api.client().paginate("search", body=Query(kind="a", limit=2), expect=decoder(Page), pages=pages)

    assert [item.id async for item in paginated] == [1, 2, 3, 4]
    assert [request.url.params.get("after") for request in api.sent] == [None, "b", "c"]
    assert len({request.content for request in api.sent}) == 1


async def test_a_body_offset_walk_advances_by_the_items_received_until_a_short_page() -> None:
    """Start at the body's offset, advance it by each page's item count, and stop at a page shorter than the size."""
    api = _Api(_by_body_offset(total=7))
    first = Query(kind="a", limit=2, offset=1)
    paginated = api.client().paginate("search", body=first, expect=decoder(Page), pages=QUERY_OFFSET)

    assert [item.id async for item in paginated] == [1, 2, 3, 4, 5, 6]
    assert [body.offset for body in api.bodies(Query)] == [1, 3, 5, 7]


async def test_a_body_offset_walk_stops_at_an_empty_page() -> None:
    """Stop at an empty page: a full page before it means one more request, which comes back empty."""
    api = _Api(_by_body_offset(total=4))
    first = Query(kind="a", limit=2)

    assert [
        item.id async for item in api.client().paginate("search", body=first, expect=decoder(Page), pages=QUERY_OFFSET)
    ] == [
        0,
        1,
        2,
        3,
    ]
    assert [body.offset for body in api.bodies(Query)] == [0, 2, 4]


@pytest.mark.parametrize("last", [None, ""], ids=["none", "empty"])
async def test_a_body_cursor_walk_stops_when_no_cursor_follows(last: str | None) -> None:
    """End the walk at a page whose cursor is ``None`` or ``""``."""

    def answer(request: httpx2.Request) -> httpx2.Response:
        return _page(request, [1], last)

    api = _Api(answer)
    first = Query(kind="a", limit=2)

    assert [
        item.id async for item in api.client().paginate("search", body=first, expect=decoder(Page), pages=QUERY_CURSOR)
    ] == [1]
    assert len(api.sent) == 1


def _with_an_empty_page(request: httpx2.Request) -> httpx2.Response:
    """Answer cursor ``None`` with an item, ``1`` with none and ``2`` with an item and no cursor."""
    at = json.loads(request.content).get("cursor")
    pages = {None: ([0], "1"), "1": ([], "2"), "2": ([2], None)}
    ids, following = pages[at]
    return _page(request, ids, following)


async def test_a_body_cursor_page_with_no_items_ends_the_walk() -> None:
    """Stop at a page with no items, though it names a cursor: a cursor that advances while returning nothing."""
    api = _Api(_with_an_empty_page)
    first = Query(kind="a", limit=2)

    assert [
        item.id async for item in api.client().paginate("search", body=first, expect=decoder(Page), pages=QUERY_CURSOR)
    ] == [0]
    assert len(api.sent) == len(("the page with an item", "the empty page"))


async def test_a_body_cursor_walk_can_go_past_an_empty_page() -> None:
    """Follow the cursor past an empty page with ``stop_on_empty=False``."""
    api = _Api(_with_an_empty_page)
    first = Query(kind="a", limit=2)
    pages = replace(QUERY_CURSOR, stop_on_empty=False)

    assert [
        item.id async for item in api.client().paginate("search", body=first, expect=decoder(Page), pages=pages)
    ] == [0, 2]


#: Pages a bounded test walk is allowed.
_BUDGET: Final = 3


async def test_a_walk_of_posts_past_its_page_limit_raises_naming_the_post() -> None:
    """Raise ``PageLimitError`` for the POST once the budget is spent with pages to come."""
    api = _Api(_endless)
    first = Query(kind="a", limit=1)
    paginated = api.client().paginate(
        "search", body=first, expect=decoder(Page), pages=QUERY_CURSOR, limit=PageLimit(_BUDGET)
    )

    items: list[int] = []
    with pytest.raises(PageLimitError) as raised:
        items.extend([item.id async for item in paginated])

    assert len(api.sent) == _BUDGET, "the page past the limit was never fetched"
    assert (raised.value.pages, raised.value.method, raised.value.url) == (_BUDGET, "POST", "search")


async def test_a_walk_of_posts_can_stop_quietly_at_its_page_limit() -> None:
    """End the walk at the budget with ``on_limit="stop"``, as a runaway backstop that does not raise."""
    api = _Api(_endless)
    first = Query(kind="a", limit=1)
    limit = PageLimit(_BUDGET, on_limit="stop")
    paginated = api.client().paginate("search", body=first, expect=decoder(Page), pages=QUERY_CURSOR, limit=limit)

    assert [item.id async for item in paginated] == list(range(_BUDGET))
    assert len(api.sent) == _BUDGET


async def test_a_repeating_body_cursor_raises_instead_of_looping() -> None:
    """Raise ``ClientError`` for the POST when a cursor leads back to a body already sent."""

    def answer(request: httpx2.Request) -> httpx2.Response:
        return _page(request, [1], "same")

    first = Query(kind="a", limit=1)
    paginated = _Api(answer).client().paginate("search", body=first, expect=decoder(Page), pages=QUERY_CURSOR)

    with pytest.raises(ClientError, match="pagination loop") as raised:
        _ = [page async for page in paginated.pages()]
    assert (raised.value.method, raised.value.url) == ("POST", "search")


#: A 503, then the pages: the first send of the first page fails after it was written.
def _flaky() -> Callable[[httpx2.Request], httpx2.Response]:
    """Answer the first request with a retryable 503, and every later one from the cursor pages."""
    calls: list[None] = []

    def answer(request: httpx2.Request) -> httpx2.Response:
        calls.append(None)
        return httpx2.Response(503, request=request) if len(calls) == 1 else _by_body_cursor(request)

    return answer


async def test_a_post_page_is_not_resent_by_default() -> None:
    """Send a POST page once, raising its 503: a POST is not idempotent, and nothing made it safe to re-send."""
    api = _Api(_flaky())
    first = Query(kind="a", limit=2)

    with pytest.raises(UpstreamStatusError) as raised:
        _ = [
            item async for item in api.client().paginate("search", body=first, expect=decoder(Page), pages=QUERY_CURSOR)
        ]

    assert len(api.sent) == 1
    assert (raised.value.method, raised.value.status_code) == ("POST", 503)


async def test_a_post_page_is_resent_under_a_policy_that_treats_post_as_idempotent() -> None:
    """Re-send a POST page when the call's ``retry=`` opts in, as a query endpoint that happens to POST may."""
    api = _Api(_flaky())
    first = Query(kind="a", limit=2)
    retry = RetryPolicy(methods=IDEMPOTENT_METHODS | {"POST"})
    paginated = api.client().paginate("search", body=first, expect=decoder(Page), pages=QUERY_CURSOR, retry=retry)

    assert [item.id async for item in paginated] == [1, 2, 3, 4]
    assert len(api.sent) == len(("the 503", "page 1", "page 2", "page 3"))


@pytest.mark.transport_policy
async def test_each_post_page_gets_its_own_idempotency_key() -> None:
    """Stamp a fresh key on each page under ``IdempotencyKeys``, so a page is re-sent with its own key."""
    api = _Api(_flaky(), policy=TransportPolicy(breaker=None, idempotency=IdempotencyKeys()))
    first = Query(kind="a", limit=2)
    paginated = api.client().paginate("search", body=first, expect=decoder(Page), pages=QUERY_CURSOR)

    assert [item.id async for item in paginated] == [1, 2, 3, 4]
    keys = [request.headers["idempotency-key"] for request in api.sent]
    assert keys[0] == keys[1], "the re-send of page 1 carries page 1's key"
    assert len(set(keys)) == len(("page 1", "page 2", "page 3"))


async def test_a_declared_answer_is_a_post_page() -> None:
    """Read a status the call declared with ``Expect.on`` as a page, as on a GET walk."""

    def answer(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(404, request=request)

    first = Query(kind="a", limit=2)
    expect = Expect(decoder(Page)).on(404, lambda _resp: Page([]))
    paginated = _Api(answer).client().paginate("search", body=first, expect=expect, pages=QUERY_CURSOR)

    assert [page async for page in paginated.pages()] == [Page([])]


async def test_an_undeclared_status_on_a_post_page_raises() -> None:
    """Raise a status the call did not declare, naming the POST."""

    def answer(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(404, request=request)

    first = Query(kind="a", limit=2)
    paginated = _Api(answer).client().paginate("search", body=first, expect=decoder(Page), pages=QUERY_CURSOR)

    with pytest.raises(UpstreamStatusError) as raised:
        _ = [page async for page in paginated.pages()]
    assert (raised.value.method, raised.value.status_code) == ("POST", 404)


@pytest.mark.parametrize("pages", [QUERY_CURSOR, QUERY_OFFSET], ids=["body-cursor", "body-offset"])
def test_a_body_pager_refuses_a_walk_of_gets(pages: Pager[Page, Item, Query]) -> None:
    """Refuse a body pager on a walk with no body: there is nothing to rewrite."""
    with pytest.raises(ValueError, match="rewrites the request body, so its walk needs one"):
        pages.first(PageRequest[Query]("search"))


@pytest.mark.parametrize(
    ("pages", "name"),
    [(LinkHeader(items=items_of), "LinkHeader"), (NextUrl(items=items_of, next=cursor_of), "NextUrl")],
    ids=["link-header", "next-url"],
)
def test_a_url_pager_refuses_a_walk_of_posts(pages: Pager[Page, Item], name: str) -> None:
    """Refuse a body for a pager that follows next-page URLs, which it fetches with GETs."""
    # A typed walk cannot pair a body with these pagers at all; an untyped one is refused when it starts.
    request = replace(PageRequest("search"), body={"kind": "a"})
    message = f"^{name} follows next-page URLs with GETs, so its walk takes no request body$"
    with pytest.raises(ValueError, match=message):
        pages.first(request)


def test_a_body_pager_refuses_a_page_without_a_body() -> None:
    """Refuse to rewrite a request that lost its body mid-walk, as a custom ``first`` could make one."""
    with pytest.raises(ValueError, match="rewrites the request body"):
        QUERY_OFFSET.next_page(Page([Item(1), Item(2)]), PageRequest[Query]("search"), httpx2.Response(200))


def test_a_body_offset_refuses_a_negative_starting_offset() -> None:
    """Refuse a first body whose offset is negative."""
    with pytest.raises(ValueError, match=r"^the first request body's offset must be >= 0 \(got -1\)$"):
        QUERY_OFFSET.first(PageRequest("search", body=Query(kind="a", limit=2, offset=-1)))


def test_a_body_offset_refuses_a_page_size_below_one() -> None:
    """Refuse a page size that could not page."""
    with pytest.raises(ValueError, match=r"^page_size must be >= 1 \(got 0\)$"):
        BodyOffset[Page, Item, Query](items=items_of, page_size=0, offset=offset_of, put=at_offset)


#: A fixed idempotency key, as a call option and as a header.
_FIXED_KEYS: Final[list[PageOptions]] = [{"idempotency_key": "k"}, {"headers": {"idempotency-key": "k"}}]


@pytest.mark.parametrize("options", _FIXED_KEYS, ids=["idempotency-key", "header"])
def test_a_walk_of_posts_refuses_one_idempotency_key_for_every_page(options: PageOptions) -> None:
    """Refuse a fixed idempotency key on a walk of POSTs: the server would replay the first page for every other."""
    first = Query(kind="a", limit=2)
    with pytest.raises(ValueError, match="idempotency key would be sent with every page's body"):
        _Api(_by_body_cursor).client().paginate(
            "search", body=first, expect=decoder(Page), pages=QUERY_CURSOR, **options
        )


def test_a_walk_of_gets_may_carry_an_idempotency_header() -> None:
    """Leave a GET walk's headers alone: no body, no replayed answer."""
    client = _Api(_by_body_cursor).client()
    pages = Cursor[Page, Item](items=items_of, next=cursor_of, param="after")
    client.paginate("items", expect=decoder(Page), pages=pages, headers={"Idempotency-Key": "k"})


async def test_a_walk_of_posts_takes_a_json_body_beside_the_url() -> None:
    """Walk POSTs from ``json=``, a body of builtins sent as JSON, as ``post(json=...)`` sends one."""

    def put(body: dict[str, object], cursor: str) -> dict[str, object]:
        return {**body, "cursor": cursor}

    api = _Api(_by_body_cursor)
    pages = BodyCursor[Page, Item, dict[str, object]](items=items_of, next=cursor_of, put=put)
    query: dict[str, object] = {"kind": "a"}
    paginated = api.client().paginate("search", json=query, expect=decoder(Page), pages=pages)

    assert [item.id async for item in paginated] == [1, 2, 3, 4]
    assert api.bodies(dict[str, object]) == [{"kind": "a"}, {"kind": "a", "cursor": "b"}, {"kind": "a", "cursor": "c"}]


#: The body arguments ``paginate`` refuses, each with the reason it gives.
_BAD_BODY_ARGUMENTS: Final[list[tuple[dict[str, object], str]]] = [
    ({"json": {}, "body": Query(kind="a", limit=2)}, r"^paginate takes body= or json=, not both$"),
    ({"data": {}}, r"^paginate takes no data=, content= or files=: a walk of POSTs sends body= or json=$"),
]


@pytest.mark.parametrize(("arguments", "message"), _BAD_BODY_ARGUMENTS, ids=["both", "data"])
def test_a_bad_body_argument_is_refused(arguments: dict[str, object], message: str) -> None:
    """Refuse two bodies, and a body no pager can rewrite."""
    with pytest.raises(ValueError, match=message):
        _Api(_by_body_cursor).client().paginate("search", expect=decoder(Page), pages=QUERY_CURSOR, **arguments)  # ty: ignore[no-matching-overload]


#: A cursor walk over :class:`Query` bodies with no writer, so a ``body=`` has nothing to encode it.
_UNWRITTEN: Final = BodyCursor[Page, Item, Query](items=items_of, next=cursor_of, put=with_cursor)


def test_a_body_whose_pager_has_no_writer_is_refused() -> None:
    """Refuse a ``body=`` that no ``write`` encodes when the walk is built, rather than send it as JSON."""
    client = _Api(_by_body_cursor).client()
    message = r"^a POSTed body is encoded by its pager's write=, so the pager needs one; send builtins as json=$"
    with pytest.raises(ValueError, match=message):
        client.paginate("search", body=Query(kind="a", limit=2), expect=decoder(Page), pages=_UNWRITTEN)


def test_json_whose_pager_has_a_writer_is_refused() -> None:
    """Refuse a ``json=`` under a pager with a ``write`` when the walk is built, rather than encode builtins with it."""
    query: dict[str, object] = {"kind": "a"}
    pages = Cursor[Page, Item, dict[str, object]](items=items_of, next=cursor_of, param="after", write=QUERY)  # ty: ignore[invalid-argument-type]
    client = _Api(_by_body_cursor).client()
    with pytest.raises(ValueError, match=r"^json= is sent as JSON, so its pager takes no write=$"):
        client.paginate("search", json=query, expect=decoder(Page), pages=pages)


def test_a_pagers_refusal_is_raised_when_the_walk_is_built() -> None:
    """Raise a pager's refusal from ``paginate`` itself, before any iteration: here a body pager given no body."""
    client = _Api(_by_body_cursor).client()
    with pytest.raises(ValueError, match="BodyCursor rewrites the request body, so its walk needs one"):
        client.paginate("search", expect=decoder(Page), pages=QUERY_CURSOR)  # ty: ignore[invalid-argument-type]


class _Rebuilt(WritingPager[Page, Item, Query]):
    """A writing body cursor whose ``next_page`` builds each request anew rather than deriving it."""

    @override
    @property
    def write(self) -> Writer[Query]:
        """Encode each page's body as a :class:`Query`."""
        return QUERY

    @override
    def first(self, request: PageRequest[Query]) -> PageRequest[Query]:
        """Return *request* unchanged."""
        return request

    @override
    def page_items(self, page: Page) -> list[Item]:
        """Return the items on *page*."""
        return page.data

    @override
    def next_page(
        self, page: Page, request: PageRequest[Query], response: httpx2.Response
    ) -> PageRequest[Query] | None:
        """Return a new request for the next cursor, built from scratch."""
        del response
        assert request.body is not None
        return (
            None
            if page.cursor is None
            else PageRequest(request.url, request.params, with_cursor(request.body, page.cursor))
        )


async def test_every_page_is_written_as_the_first_was() -> None:
    """Write every page with the pager's writer, whatever request the pager returns."""
    api = _Api(_by_body_cursor)
    paginated = api.client().paginate("search", body=Query(kind="a", limit=2), expect=decoder(Page), pages=_Rebuilt())

    assert [item.id async for item in paginated] == [1, 2, 3, 4]
    assert [body.cursor for body in api.bodies(Query)] == [None, "b", "c"]


async def test_a_body_given_as_none_is_no_body() -> None:
    """Take ``body=None`` beside ``json=`` as one body, as a wrapper forwarding optional arguments passes them."""

    def put(body: dict[str, object], cursor: str) -> dict[str, object]:
        return {**body, "cursor": cursor}

    api = _Api(_by_body_cursor)
    pages = BodyCursor[Page, Item, dict[str, object]](items=items_of, next=cursor_of, put=put)
    query: dict[str, object] = {"kind": "a"}
    paginated = api.client().paginate("search", body=None, json=query, expect=decoder(Page), pages=pages)  # ty: ignore[no-matching-overload]

    assert [item.id async for item in paginated] == [1, 2, 3, 4]


async def test_an_offset_walk_of_posts_writes_its_body_on_every_page() -> None:
    """Page a POST by a query-parameter offset, writing the same body on every page with the pager's writer."""
    total = 5

    def answer(request: httpx2.Request) -> httpx2.Response:
        start = int(request.url.params["offset"])
        return _page(request, [i for i in range(start, start + 2) if i < total])

    api = _Api(answer)
    pages = Offset[Page, Item, Query](items=items_of, page_size=2, write=QUERY)
    paginated = api.client().paginate("search", body=Query(kind="a", limit=2), expect=decoder(Page), pages=pages)

    assert [item.id async for item in paginated] == [0, 1, 2, 3, 4]
    assert [request.url.params["offset"] for request in api.sent] == ["0", "2", "4"]
    assert {request.headers["content-type"] for request in api.sent} == {"application/json"}
    assert api.bodies(Query) == [Query(kind="a", limit=2)] * 3


#: A query-parameter cursor that writes a :class:`Query` body when the walk has one.
QUERY_PARAM_CURSOR: Final = Cursor[Page, Item, Query](items=items_of, next=cursor_of, param="after", write=QUERY)


async def _search(client: StubClient, query: Query | None = None) -> list[int]:
    """Search with *query*, or list everything without one: one call, whether the body is given or not."""
    paginated = client.paginate("search", body=query, expect=decoder(Page), pages=QUERY_PARAM_CURSOR)
    return [item.id async for item in paginated]


def _by_param_cursor(request: httpx2.Request) -> httpx2.Response:
    """Serve the cursor pages by the ``after`` query parameter, whatever the method."""
    ids, following = _CURSOR_PAGES[request.url.params.get("after")]
    return _page(request, ids, following)


async def test_an_optional_body_is_one_call() -> None:
    """Walk POSTs when the optional body is given and GETs when it is ``None``, under the same writing pager."""
    api = _Api(_by_param_cursor)

    assert await _search(api.client(), Query(kind="a", limit=2)) == [1, 2, 3, 4]
    assert await _search(api.client()) == [1, 2, 3, 4]
    assert [request.method for request in api.sent] == ["POST"] * 3 + ["GET"] * 3


class _PostsLater:
    """A pager of GETs that turns into POSTs of a :class:`Query` on its second page, writing none."""

    def first(self, request: PageRequest[Query]) -> PageRequest[Query]:
        """Return *request* unchanged."""
        return request

    def page_items(self, page: Page) -> list[Item]:
        """Return the items on *page*."""
        return page.data

    def next_page(
        self, page: Page, request: PageRequest[Query], response: httpx2.Response
    ) -> PageRequest[Query] | None:
        """Return the next request, POSTing a body from the second page on."""
        del response
        return None if page.cursor is None else request.with_body(Query(kind="a", limit=2, cursor=page.cursor))


class _WrittenPostsLater(_PostsLater, WritingPager[Page, Item, Query]):
    """A :class:`_PostsLater` that writes its bodies as a :class:`Query`."""

    @override
    @property
    def write(self) -> Writer[Query]:
        """Encode each page's body as a :class:`Query`."""
        return QUERY


@pytest.mark.parametrize(
    ("pages", "options", "message"),
    [
        (_PostsLater(), {}, "a POSTed body is encoded by its pager's write="),
        (_WrittenPostsLater(), {"idempotency_key": "k"}, "idempotency key would be sent with every page's body"),
    ],
    ids=["no-writer", "fixed-key"],
)
async def test_a_body_that_appears_mid_walk_is_held_to_the_same_rules(
    pages: _PostsLater, options: PageOptions, message: str
) -> None:
    """Refuse a later page's body that cannot be sent as the walk says, from the iteration, before sending it."""
    api = _Api(_by_param_cursor)
    paginated = api.client().paginate("search", expect=decoder(Page), pages=pages, **options)  # ty: ignore[invalid-argument-type]

    with pytest.raises(ValueError, match=message):
        _ = [item async for item in paginated]
    assert [request.method for request in api.sent] == ["GET"]


class _Logged:
    """A cursor over JSON bodies whose ``write`` is a log's, not a body writer: it is not a :class:`WritingPager`."""

    def __init__(self) -> None:
        """Keep a log, whose ``write`` happens to share the writer's name."""
        self.log = io.StringIO()
        self.write = self.log.write

    def first(self, request: PageRequest[dict[str, object]]) -> PageRequest[dict[str, object]]:
        """Return *request* unchanged."""
        return request

    def page_items(self, page: Page) -> list[Item]:
        """Return the items on *page*."""
        return page.data

    def next_page(
        self, page: Page, request: PageRequest[dict[str, object]], response: httpx2.Response
    ) -> PageRequest[dict[str, object]] | None:
        """Return *request* with *page*'s cursor in its body, or ``None`` after the last page."""
        del response
        assert request.body is not None
        return None if page.cursor is None else request.with_body({**request.body, "cursor": page.cursor})


async def test_an_attribute_named_write_is_not_a_writer() -> None:
    """Send a ``json=`` walk as JSON under a pager that has a ``write`` attribute but is not a writing pager."""
    pages = _Logged()
    api = _Api(_by_body_cursor)
    query: dict[str, object] = {"kind": "a"}
    paginated = api.client().paginate("search", json=query, expect=decoder(Page), pages=pages)

    assert [item.id async for item in paginated] == [1, 2, 3, 4]
    assert pages.log.getvalue() == ""
