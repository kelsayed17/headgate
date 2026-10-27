# Pagination

`client.paginate(url, *, expect, pages, retry=True, limit=None, **options)` follows a paginated endpoint and returns a
`Paginated[P, I]`: `async for` yields the items `I`, and `.pages()` yields the pages `P`. Each page is a GET through
the whole stack (rate budget, retries, breaker, cache, credential rules), fetched only when the iteration reaches it.
An endpoint whose query is a request body is walked with POSTs instead: see [Walks of POSTs](#walks-of-posts).

```python
from headgate import Cursor
from headgate.msgspec import decoder


def items_of(page: Page) -> list[Item]:
    return page.data


def cursor_of(page: Page) -> str | None:
    return page.next_cursor


ITEM_PAGES = Cursor(items=items_of, next=cursor_of, param="after")

async for item in client.paginate("items", expect=decoder(Page), pages=ITEM_PAGES, params={"limit": 100}):
    ...
```

Nothing is fetched until iteration starts, no page is kept once the iteration moves past it, stopping early fetches
nothing more, and iterating again starts again from the first page. A page that leads back to a request already made
(a cursor or next URL that repeats) raises `ClientError` instead of looping forever.

## Collecting into a list

`await walk.collect()` returns every item as a list. `collect(max_items=n)` returns at most the first `n`, and stops as
soon as it has them: the page after the one that filled the list is never requested.

```python
everything = await client.paginate("items", expect=decoder(Page), pages=ITEM_PAGES).collect()
first_250 = await client.paginate("items", expect=decoder(Page), pages=ITEM_PAGES).collect(max_items=250)
```

`max_items=0` returns `[]` without a request, and a negative count raises `ValueError`. Use `max_items` to bound how
many **items** you get back, and a [page budget](#a-page-budget) to bound how many **requests** a walk may make; when
both apply, a `PageLimitError` is raised only if the budget runs out before `max_items` is reached.

## A page budget

A walk ends at its last page, but a server bug, an unordered dataset or a too-broad filter can keep naming new pages
for as long as you keep asking. `limit=PageLimit(max_pages)` bounds the walk:

```python
from headgate import PageLimit, PageLimitError
from headgate.msgspec import decoder

orders = client.paginate("orders", expect=decoder(Page), pages=ORDER_PAGES, limit=PageLimit(50))
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

`paginate` refuses body options (`body`, `json`, `data`, `content`, `files`) with `ValueError`: a walk of POSTs passes
its body in a `PageRequest` instead (see [Walks of POSTs](#walks-of-posts)). Its `params=` are the first page's query,
and a pager's parameter is added to them. `expect=` may be an `Expect`: a page can be a
declared answer like any other. Every other option applies to each page as it does to one `get`: `follow_redirects=False`
makes a page's redirect raise as its 3xx, and `cache_ttl=` is each page's cache lifetime.

## Strategies

| Strategy | The next page is | Last page |
| --- | --- | --- |
| `Cursor(items=, next=, param=)` | the same request with `param` set to the cursor `next(page)` returns | `next` returns `None` or `""`, or a page with no items |
| `Offset(items=, page_size=, param="offset", start=0)` | the same request with `param` advanced by the items this page held | a page with fewer than `page_size` items |
| `LinkHeader(items=)` | the target of the response's `Link: <...>; rel="next"` (RFC 8288) | no `rel="next"` link, or a page with no items |
| `NextUrl(items=, next=)` | the URL `next(page)` returns, absolute or relative | `next` returns `None` or `""`, or a page with no items |
| `BodyCursor(items=, next=, put=)` | the same POST with its body `put(body, cursor)` | as `Cursor` |
| `BodyOffset(items=, page_size=, offset=, put=)` | the same POST with its body `put(body, offset(body) + items)` | as `Offset` |

A page with no items ends a `Cursor`, `LinkHeader` or `NextUrl` walk even when it links to another: a link that
advances while returning nothing makes no progress, and following it spends the rate budget for nothing. For an
endpoint that sends empty pages mid-walk, pass `stop_on_empty=False`.

`Offset` sends only the offset; the page size goes in `params=`, under whatever name the endpoint uses (`limit`,
`per_page`, `page_size`). A next URL from the wire (`LinkHeader`, `NextUrl`) is resolved against the URL that answered
and sent as an absolute URL, under the usual rules: credentials go only to [trusted origins](security.md), and the SSRF
guard vets it when it is on.

Each strategy is a frozen value: build it once, next to the client, as with readers.

## Walks of POSTs

Some endpoints take their query as a request body, so the page token or offset travels in the body too: a search API
that is a POST, or a JSON-RPC call whose arguments carry a cursor. `paginate` walks one when its first argument is a
`PageRequest` with a `body`. The body stays your own value (a struct, or builtins), so a pager can rewrite it for each
next page; `write=` is the writer that encodes it, a codec's `encoder` as for a verb's `body=`, and without it the body
is sent as `json=`.

```python
import msgspec

from headgate import BodyCursor, BodyOffset
from headgate.msgspec import decoder, encoder
from headgate.pagination import PageRequest

QUERY = encoder(Query)


def with_cursor(query: Query, cursor: str) -> Query:
    return msgspec.structs.replace(query, cursor=cursor)


SEARCH_PAGES = BodyCursor[Page, Item, Query](items=items_of, next=cursor_of, put=with_cursor)

first = PageRequest("search", body=Query(kind="open", limit=100), write=QUERY)
async for item in client.paginate(first, expect=decoder(Page), pages=SEARCH_PAGES):
    ...
```

`BodyCursor` posts the first body as you wrote it, then `put(body, cursor)` with each cursor `next` reads from a page.
`BodyOffset` reads the offset the body carries with `offset(body)`, starts there, and posts `put(body, offset)` past
each full page; as with `Offset`, the page size is yours to put in the body, and a page shorter than `page_size` ends
the walk:

```python
def offset_of(query: Query) -> int:
    return query.offset


def at_offset(query: Query, offset: int) -> Query:
    return msgspec.structs.replace(query, offset=offset)


ROW_PAGES = BodyOffset[list[Row], Row, Query](items=rows_of, page_size=5000, offset=offset_of, put=at_offset)
```

`put` returns a new body and leaves the one it was given alone, so the caller's body is never changed under it. The
`PageRequest` carries the first page's query too (`PageRequest(url, params, body=...)`), so `paginate` refuses
`params=` beside one. `Cursor` and `Offset` page a POST as well, by query parameter, sending the same body with each
page: name the body type, as in `Cursor[Page, Item, Query]`. `LinkHeader` and `NextUrl` refuse a body, since the URL a
response names is fetched with a GET; `BodyCursor` and `BodyOffset` refuse a walk with no body, since there is nothing
for them to rewrite.

Each page is then a `post` in every respect: the same stack, `expect=` (with its declared answers), breaker and page
budget. That includes the [retry rule](retries.md) for a method that is not idempotent: a POST page that fails after it
was sent is not re-sent unless something makes that safe. Opt in with `TransportPolicy(idempotency=IdempotencyKeys())`,
which stamps a fresh key on each page (and the same key on that page's re-sends), or, for a read that merely travels
as a POST, with `retry=RetryPolicy(methods=IDEMPOTENT_METHODS | {"POST"})`. A fixed key, `idempotency_key=` or the
key's header in `headers=`, is refused on a walk of POSTs: every page would carry it, and a server that honors it
would answer each page with the first.

## A pager of your own

A scheme the strategies do not cover implements the `Pager[P, I]` protocol (`Pager[P, I, B]` for a walk of POSTs whose
bodies are of type `B`):

```python
import httpx2

from headgate.pagination import PageRequest


class PageNumbers:
    """Pages addressed by number, ending at the page count the body reports."""

    def first(self, request: PageRequest) -> PageRequest:
        return request.with_param("page", 1)

    def page_items(self, page: NumberedPage) -> list[Item]:
        return page.items

    def next_page(self, page: NumberedPage, request: PageRequest, response: httpx2.Response) -> PageRequest | None:
        return request.with_param("page", page.number + 1) if page.number < page.total_pages else None
```

`PageRequest(url, params)` is the GET for one page, and `PageRequest(url, params, body=, write=)` the POST;
`with_param(name, value)` returns it with one query parameter set, and `with_body(body)` with another body.
`next_page` sees the page, the request that fetched it and the response (for its headers).
