# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""Walks of POSTs: ``BodyCursor`` and ``BodyOffset`` page through the request body, and every page is a ``post``.

An in-memory API reads each page's JSON body and answers from it, so what a walk rewrote shows in what was sent.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Final, assert_type

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
    PageLimit,
    PageLimitError,
    RequestOptions,
    RetryPolicy,
    TransportPolicy,
    UpstreamStatusError,
)
from headgate.msgspec import decoder, encoder
from headgate.pagination import PageRequest
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
QUERY_CURSOR: Final = BodyCursor[Page, Item, Query](items=items_of, next=cursor_of, put=with_cursor)
#: The offset walk over :class:`Query` bodies, two items a page.
QUERY_OFFSET: Final = BodyOffset[Page, Item, Query](items=items_of, page_size=2, offset=offset_of, put=at_offset)


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
    paginated = api.client().paginate(
        PageRequest("search", {"v": "1"}, first, write=QUERY), expect=decoder(Page), pages=QUERY_CURSOR
    )

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
    paginated = api.client().paginate(PageRequest("mcp", body=call), expect=decoder(Page), pages=pages)

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
    pages = Cursor[Page, Item, Query](items=items_of, next=cursor_of, param="after")
    request = PageRequest("search", body=Query(kind="a", limit=2), write=QUERY)

    assert [item.id async for item in api.client().paginate(request, expect=decoder(Page), pages=pages)] == [1, 2, 3, 4]
    assert [request.url.params.get("after") for request in api.sent] == [None, "b", "c"]
    assert len({request.content for request in api.sent}) == 1


async def test_a_body_offset_walk_advances_by_the_items_received_until_a_short_page() -> None:
    """Start at the body's offset, advance it by each page's item count, and stop at a page shorter than the size."""
    api = _Api(_by_body_offset(total=7))
    first = PageRequest("search", body=Query(kind="a", limit=2, offset=1), write=QUERY)
    paginated = api.client().paginate(first, expect=decoder(Page), pages=QUERY_OFFSET)

    assert [item.id async for item in paginated] == [1, 2, 3, 4, 5, 6]
    assert [body.offset for body in api.bodies(Query)] == [1, 3, 5, 7]


async def test_a_body_offset_walk_stops_at_an_empty_page() -> None:
    """Stop at an empty page: a full page before it means one more request, which comes back empty."""
    api = _Api(_by_body_offset(total=4))
    first = PageRequest("search", body=Query(kind="a", limit=2), write=QUERY)

    assert [item.id async for item in api.client().paginate(first, expect=decoder(Page), pages=QUERY_OFFSET)] == [
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
    first = PageRequest("search", body=Query(kind="a", limit=2), write=QUERY)

    assert [item.id async for item in api.client().paginate(first, expect=decoder(Page), pages=QUERY_CURSOR)] == [1]
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
    first = PageRequest("search", body=Query(kind="a", limit=2), write=QUERY)

    assert [item.id async for item in api.client().paginate(first, expect=decoder(Page), pages=QUERY_CURSOR)] == [0]
    assert len(api.sent) == len(("the page with an item", "the empty page"))


async def test_a_body_cursor_walk_can_go_past_an_empty_page() -> None:
    """Follow the cursor past an empty page with ``stop_on_empty=False``."""
    api = _Api(_with_an_empty_page)
    first = PageRequest("search", body=Query(kind="a", limit=2), write=QUERY)
    pages = replace(QUERY_CURSOR, stop_on_empty=False)

    assert [item.id async for item in api.client().paginate(first, expect=decoder(Page), pages=pages)] == [0, 2]


#: Pages a bounded test walk is allowed.
_BUDGET: Final = 3


async def test_a_walk_of_posts_past_its_page_limit_raises_naming_the_post() -> None:
    """Raise ``PageLimitError`` for the POST once the budget is spent with pages to come."""
    api = _Api(_endless)
    first = PageRequest("search", body=Query(kind="a", limit=1), write=QUERY)
    paginated = api.client().paginate(first, expect=decoder(Page), pages=QUERY_CURSOR, limit=PageLimit(_BUDGET))

    items: list[int] = []
    with pytest.raises(PageLimitError) as raised:
        items.extend([item.id async for item in paginated])

    assert len(api.sent) == _BUDGET, "the page past the limit was never fetched"
    assert (raised.value.pages, raised.value.method, raised.value.url) == (_BUDGET, "POST", "search")


async def test_a_walk_of_posts_can_stop_quietly_at_its_page_limit() -> None:
    """End the walk at the budget with ``on_limit="stop"``, as a runaway backstop that does not raise."""
    api = _Api(_endless)
    first = PageRequest("search", body=Query(kind="a", limit=1), write=QUERY)
    limit = PageLimit(_BUDGET, on_limit="stop")
    paginated = api.client().paginate(first, expect=decoder(Page), pages=QUERY_CURSOR, limit=limit)

    assert [item.id async for item in paginated] == list(range(_BUDGET))
    assert len(api.sent) == _BUDGET


async def test_a_repeating_body_cursor_raises_instead_of_looping() -> None:
    """Raise ``ClientError`` for the POST when a cursor leads back to a body already sent."""

    def answer(request: httpx2.Request) -> httpx2.Response:
        return _page(request, [1], "same")

    first = PageRequest("search", body=Query(kind="a", limit=1), write=QUERY)
    paginated = _Api(answer).client().paginate(first, expect=decoder(Page), pages=QUERY_CURSOR)

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
    first = PageRequest("search", body=Query(kind="a", limit=2), write=QUERY)

    with pytest.raises(UpstreamStatusError) as raised:
        _ = [item async for item in api.client().paginate(first, expect=decoder(Page), pages=QUERY_CURSOR)]

    assert len(api.sent) == 1
    assert (raised.value.method, raised.value.status_code) == ("POST", 503)


async def test_a_post_page_is_resent_under_a_policy_that_treats_post_as_idempotent() -> None:
    """Re-send a POST page when the call's ``retry=`` opts in, as a query endpoint that happens to POST may."""
    api = _Api(_flaky())
    first = PageRequest("search", body=Query(kind="a", limit=2), write=QUERY)
    retry = RetryPolicy(methods=IDEMPOTENT_METHODS | {"POST"})
    paginated = api.client().paginate(first, expect=decoder(Page), pages=QUERY_CURSOR, retry=retry)

    assert [item.id async for item in paginated] == [1, 2, 3, 4]
    assert len(api.sent) == len(("the 503", "page 1", "page 2", "page 3"))


@pytest.mark.transport_policy
async def test_each_post_page_gets_its_own_idempotency_key() -> None:
    """Stamp a fresh key on each page under ``IdempotencyKeys``, so a page is re-sent with its own key."""
    api = _Api(_flaky(), policy=TransportPolicy(breaker=None, idempotency=IdempotencyKeys()))
    first = PageRequest("search", body=Query(kind="a", limit=2), write=QUERY)
    paginated = api.client().paginate(first, expect=decoder(Page), pages=QUERY_CURSOR)

    assert [item.id async for item in paginated] == [1, 2, 3, 4]
    keys = [request.headers["idempotency-key"] for request in api.sent]
    assert keys[0] == keys[1], "the re-send of page 1 carries page 1's key"
    assert len(set(keys)) == len(("page 1", "page 2", "page 3"))


async def test_a_declared_answer_is_a_post_page() -> None:
    """Read a status the call declared with ``Expect.on`` as a page, as on a GET walk."""

    def answer(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(404, request=request)

    first = PageRequest("search", body=Query(kind="a", limit=2), write=QUERY)
    expect = Expect(decoder(Page)).on(404, lambda _resp: Page([]))
    paginated = _Api(answer).client().paginate(first, expect=expect, pages=QUERY_CURSOR)

    assert [page async for page in paginated.pages()] == [Page([])]


async def test_an_undeclared_status_on_a_post_page_raises() -> None:
    """Raise a status the call did not declare, naming the POST."""

    def answer(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(404, request=request)

    first = PageRequest("search", body=Query(kind="a", limit=2), write=QUERY)
    paginated = _Api(answer).client().paginate(first, expect=decoder(Page), pages=QUERY_CURSOR)

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


def test_a_writer_without_a_body_is_refused() -> None:
    """Refuse a ``write`` with no body to write."""
    with pytest.raises(ValueError, match=r"^write= encodes a request body, so it needs one$"):
        PageRequest[Query]("search", write=QUERY)


def test_params_beside_a_page_request_are_refused() -> None:
    """Refuse ``params=`` beside a ``PageRequest``, which carries its own query."""
    first = PageRequest("search", body=Query(kind="a", limit=2), write=QUERY)
    with pytest.raises(ValueError, match="carries its own query"):
        _Api(_by_body_cursor).client().paginate(first, expect=decoder(Page), pages=QUERY_CURSOR, params={"v": "1"})


#: A fixed idempotency key, as a call option and as a header.
_FIXED_KEYS: Final[list[RequestOptions]] = [{"idempotency_key": "k"}, {"headers": {"idempotency-key": "k"}}]


@pytest.mark.parametrize("options", _FIXED_KEYS, ids=["idempotency-key", "header"])
def test_a_walk_of_posts_refuses_one_idempotency_key_for_every_page(options: RequestOptions) -> None:
    """Refuse a fixed idempotency key on a walk of POSTs: the server would replay the first page for every other."""
    first = PageRequest("search", body=Query(kind="a", limit=2), write=QUERY)
    with pytest.raises(ValueError, match="idempotency key would be sent with every page's body"):
        _Api(_by_body_cursor).client().paginate(first, expect=decoder(Page), pages=QUERY_CURSOR, **options)


def test_a_walk_of_gets_may_carry_an_idempotency_header() -> None:
    """Leave a GET walk's headers alone: no body, no replayed answer."""
    client = _Api(_by_body_cursor).client()
    pages = Cursor[Page, Item](items=items_of, next=cursor_of, param="after")
    client.paginate("items", expect=decoder(Page), pages=pages, headers={"Idempotency-Key": "k"})
