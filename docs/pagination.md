# Pagination

`client.paginate(url, *, expect, pages, query=None, limit=None, **options)` follows a paginated endpoint and returns a
`Paginated[P, I]`: `async for` yields the items `I`, and `.pages()` yields the pages `P`. Each page is a GET through the
whole stack (rate budget, retries, breaker, cache, credential rules), fetched only when the iteration reaches it. An
endpoint whose query is a request body is walked with POSTs instead, given that body as `query=`: see
[Walks of POSTs](#walks-of-posts).

```python
from headgate import Cursor
from headgate.msgspec import reader


def items_of(page: Page) -> list[Item]:
    return page.data


def cursor_of(page: Page) -> str | None:
    return page.next_cursor


ITEM_PAGES = Cursor(items=items_of, next=cursor_of, param="after")

async for item in client.paginate("items", expect=reader(Page), pages=ITEM_PAGES, params={"limit": 100}):
    ...
```

Nothing is fetched until iteration starts, no page is kept once the iteration moves past it, stopping early fetches
nothing more, and iterating again starts again from the first page. A page that leads back to a request already made
(a cursor or next URL that repeats) raises `ClientError` instead of looping forever. A request is remembered by its URL,
as written and as sent, its query and, for a POST, the bytes its body is sent as, so two bodies are one page exactly
when they are sent alike. `Offset`, `PageNumber` and `BodyOffset` build each request from the last, so a server that
ignores the paging parameter would answer the same page forever: a page with items whose body repeats an earlier
page's raises `ClientError` too. A walk remembers a fixed-size digest of each, for its last 4096 pages.

## Collecting into a list

`await walk.collect()` returns every item as a list. `collect(max_items=n)` returns at most the first `n`, and stops as
soon as it has them: the page after the one that filled the list is never requested.

```python
everything = await client.paginate("items", expect=reader(Page), pages=ITEM_PAGES).collect()
first_250 = await client.paginate("items", expect=reader(Page), pages=ITEM_PAGES).collect(max_items=250)
```

`max_items=0` returns `[]` without a request, and a negative count raises `ValueError`. Use `max_items` to bound how
many **items** you get back, and a [page budget](#a-page-budget) to bound how many **requests** a walk may make; when
both apply, a `PageLimitError` is raised only if the budget runs out before `max_items` is reached.

## A page budget

A walk ends at its last page, but a server bug, an unordered dataset or a too-broad filter can keep naming new pages
for as long as you keep asking. `limit=PageLimit(max_pages)` bounds the walk:

```python
from headgate import PageLimit, PageLimitError
from headgate.msgspec import reader

orders = client.paginate("orders", expect=reader(Page), pages=ORDER_PAGES, limit=PageLimit(50))
try:
    async for order in orders:
        ...
except PageLimitError as exc:
    print(f"stopped after {exc.pages} pages: the result is incomplete")
```

- **`on_limit="raise"`** (the default) raises `PageLimitError`, a `ClientError`, once the budget is spent with pages
  still to come. The pages within the budget were already yielded; `exc.pages` says how many. A caller cannot mistake
  a cut-short walk for a complete one.
- **`on_limit="stop"`** ends the walk at the budget instead, for a caller that wants only the first pages:
  `PageLimit(5, on_limit="stop")`.
- A walk whose last page falls within the budget, or exactly on it, is unaffected.

Set the budget where the client calls `paginate`, so every caller of that endpoint gets it.

A walk of POSTs takes its body as `query=` (see [Walks of POSTs](#walks-of-posts)), and no `body=`: a walk's body is the
value its pager rewrites, not one encoded body. Its `params=` are the first page's query, and a pager's parameter is
added to them. `expect=` may be an `Expect`: a page can be a declared answer like any other. Every other option applies
to each page as it does to one `get` (or one `post`, in a walk of POSTs): `follow_redirects=False` makes a page's
redirect raise as its 3xx, and `cache_ttl=` is each page's cache lifetime.

## Strategies

| Strategy | The next page is | Last page |
| --- | --- | --- |
| `Cursor(items=, next=, param=)` | the same request with `param` set to the cursor `next(page)` returns | `next` returns `None` or `""`, or a page with no items |
| `Offset(items=, page_size=, param="offset", size_param=None, start=0, total=None)` | the same request with `param` advanced by the items this page held | a page with fewer items than the page size, or one that reaches `total` |
| `PageNumber(items=, more=, param="page", start=1)` | the same request with `param` one higher | `more` returns `False`, or a page with no items |
| `LinkHeader(items=)` | the target of the response's `Link: <...>; rel="next"` (RFC 8288), unless its `anchor` names another resource | no `rel="next"` link, or a page with no items |
| `NextUrl(items=, next=)` | the URL `next(page)` returns, absolute or relative | `next` returns `None` or `""`, or a page with no items |
| `BodyCursor(items=, next=, put=)` | the same POST with its body `put(body, cursor)` | as `Cursor` |
| `BodyOffset(items=, page_size=, offset=, put=, total=None)` | the same POST with its body `put(body, offset(body) + items)` | as `Offset` |

A page with no items ends a `Cursor`, `PageNumber`, `LinkHeader` or `NextUrl` walk even when it links to another: a link that
advances while returning nothing makes no progress, and following it spends the rate budget for nothing. For an
endpoint that sends empty pages mid-walk, pass `stop_on_empty=False`.

With `size_param=`, `Offset` sends the page size too, under whatever name the endpoint uses (`limit`, `per_page`,
SODA's `$limit`): your own size there, an int of at least 1 or a string of digits, when your `params=` give one, and
`page_size` otherwise. A page shorter than that size is the last. Without `size_param=`, the page size is yours to
send in `params=`, and the walk steps by `page_size`. The walk starts at the offset your own `params=` give, else at
`start` (`1` for a 1-based API). Where a page reports how many items the whole walk holds, `total=` reads it, and the walk ends once it has
passed that many items, or at an empty page: it does not ask for the empty page past a last page that happened to be
full, and a server that caps its pages below `page_size` is still walked to the end. A `total` that returns `None` (a count the server gives only as "at least", say) leaves the walk to end at a short page:

```python
def exact_total(page: SearchPage) -> int | None:
    return page.total.value if page.total.relation == "eq" else None


SEARCH_PAGES = Offset(items=hits_of, page_size=100, param="from", total=exact_total)
```

`PageNumber` asks for page 1, 2, 3 and so on, starting at the page the caller's `params=` name (an int, or a string of
digits) or at `start` when they name none. `more` reads from a page whether another follows: a page count, a
`has_more` flag, or a `next` link read only as a yes or no:

```python
def more(page: FilingsPage) -> bool:
    return page.next is not None


FILING_PAGES = PageNumber(items=results_of, more=more)
```

Asking for the next number on your own request, rather than following the link, keeps every page on your URL with
your whole query, where a link the server built may drop a filter, or name another host. A next URL from the wire (`LinkHeader`, `NextUrl`) is resolved against the URL that answered
and sent as an absolute URL, under the usual rules: credentials go only to [trusted origins](security.md), and the SSRF
guard vets it when it is on.

Each strategy is a frozen value: build it once, next to the client, as with readers.

## Walks of POSTs

Some endpoints take their query as a request body, so the page token or offset travels in the body too: a search API
that is a POST, or a JSON-RPC call whose arguments carry a cursor. `paginate` walks one when it is given a body as
`query=`: your own value, which the pager rewrites page by page, so it needs the value, not its bytes. Each page's body
is encoded by the pager's `write=` (a flavor's `writer`): the writer belongs to the endpoint, like the reader and the
pager, so it is built once beside them.

```python
import msgspec

from headgate import BodyCursor, BodyOffset
from headgate.msgspec import reader, writer

QUERY = writer(Query)


def with_cursor(query: Query, cursor: str) -> Query:
    return msgspec.structs.replace(query, cursor=cursor)


SEARCH_PAGES = BodyCursor[Page, Item, Query](items=items_of, next=cursor_of, put=with_cursor, write=QUERY)

query = Query(kind="open", limit=100)
async for item in client.paginate("search", query=query, expect=reader(Page), pages=SEARCH_PAGES):
    ...
```

A body of builtins needs no writer: leave out `write=` and have `put` return a new dict, and each page's body is sent as
JSON. `query=None` is no body, so a method with an optional body makes one call: under a `Cursor` or `Offset` the walk
is then GETs, and the writer goes unused. A body of another type than the pager's, and a body on a GET-only pager
(`LinkHeader`, `NextUrl`), are type errors. A body pager given no body raises `ValueError` from the `paginate` call
itself, before anything is fetched, and so does `body=` or `json=`, which a walk does not take, with a `TypeError`.

`BodyCursor` posts the first body as you wrote it, then `put(body, cursor)` with each cursor `next` reads from a page.
`BodyOffset` reads the offset the body carries with `offset(body)`, starts there, and posts `put(body, offset)` past
each full page; as with `Offset`, the page size is yours to put in the body, and a page shorter than `page_size` ends
the walk:

```python
def offset_of(query: Query) -> int:
    return query.offset


def at_offset(query: Query, offset: int) -> Query:
    return msgspec.structs.replace(query, offset=offset)


ROW_PAGES = BodyOffset[list[Row], Row, Query](
    items=rows_of, page_size=5000, offset=offset_of, put=at_offset, write=QUERY
)
```

`put` returns a new body and leaves the one it was given alone, so the caller's body is never changed under it.
`Cursor` and `Offset` page a POST as well, by query parameter, sending the same body with each page: give them the
body's type and writer, as in `Cursor[Page, Item, Query](..., write=QUERY)`, and pass `query=`, or leave the writer out
for builtins sent as JSON. `LinkHeader` and `NextUrl` refuse a body, since the URL a response names is fetched with a GET;
`BodyCursor` and `BodyOffset` refuse a walk with no body, since there is nothing for them to rewrite.

Each page is then a `post` in every respect: the same stack, `expect=` (with its declared answers), breaker and page
budget. That includes the [retry rule](retries.md) for a method that is not idempotent: a POST page that fails after it
was sent is not re-sent unless something makes that safe. Opt in with `TransportPolicy(idempotency=IdempotencyKeys())`,
which stamps a fresh key on each page (and the same key on that page's re-sends), or, for a read that merely travels
as a POST, with `retry=RetryPolicy(methods=IDEMPOTENT_METHODS | {"POST"})`. A fixed key, `idempotency_key=` or the
key's header in `headers=`, is refused on a walk of POSTs: every page would carry it, and a server that honors it
would answer each page with the first.

## A pager of your own

A scheme the strategies do not cover implements the `Pager[P, I]` protocol (`Pager[P, I, B]` for a walk of POSTs whose
bodies are of type `B`). One that encodes its walk's bodies subclasses `WritingPager[P, I, B]` and gives it a `write`
property:
a pager writes bodies because it says so, not because it has an attribute of that name.

```python
import httpx2

from headgate.pagination import PageRequest


class HeaderTokens:
    """Pages linked by a token the response sends in a header, sent back as the query parameter ``token``."""

    def first(self, request: PageRequest) -> PageRequest:
        return request

    def page_items(self, page: list[Item]) -> list[Item]:
        return page

    def next_page(self, page: list[Item], request: PageRequest, response: httpx2.Response) -> PageRequest | None:
        token = response.headers.get("X-Next-Page-Token")
        return request.with_param("token", token) if token else None
```

A pager that encodes its walk's bodies subclasses `WritingPager` instead, which makes every member abstract, so leaving
one out is an error when the pager is built rather than a `None` mid-walk. A frozen dataclass field named `write` gives
the writer, as the strategies do. Here it has no default, since this endpoint's bodies are not builtins
(`SearchPages(write=writer(Query))`):

```python
from dataclasses import dataclass
from typing import override

import httpx2
import msgspec

from headgate import Writer
from headgate.pagination import PageRequest, WritingPager


@dataclass(frozen=True, slots=True, kw_only=True)
class SearchPages(WritingPager[SearchPage, Item, Query]):
    """Pages of a search whose body carries the page number."""

    write: Writer[Query]

    @override
    def first(self, request: PageRequest[Query]) -> PageRequest[Query]:
        return request

    @override
    def page_items(self, page: SearchPage) -> list[Item]:
        return page.items

    @override
    def next_page(
        self, page: SearchPage, request: PageRequest[Query], response: httpx2.Response
    ) -> PageRequest[Query] | None:
        if request.body is None or page.number >= page.total_pages:
            return None
        return request.with_body(msgspec.structs.replace(request.body, page=page.number + 1))
```

A pager speaks `PageRequest`: `paginate` builds the first from its URL, `params=` and body, `first` prepares it once
(so its refusals raise from `paginate`, and it depends on the request alone), and the pager returns each next.
`PageRequest(url, params)` is the GET for one page, and `PageRequest(url, params, body=)` the POST. A pager that
returns a next URL it read from the wire wraps it in `httpx2.URL`; a literal or a t-string is as valid as in a verb.
How a body is sent is the walk's, not the request's: a writing pager's `write` encodes every page's body, whatever
request the pager returns. Every page with a body is held to the same rules as the first, so a pager that turns a walk
of GETs into POSTs halfway raises `ValueError` from the iteration if its body cannot be sent.
`with_param(name, value)` returns a request with one query parameter set, and `with_body(body)` with another body.
`next_page` sees the page, the request that fetched it and the response (for its headers).
