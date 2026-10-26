# Pagination

`client.paginate(url, *, expect, pages, retry=True, **options)` follows a paginated endpoint and returns a
`Paginated[P, I]`: `async for` yields the items `I`, and `.pages()` yields the pages `P`. Each page is a GET through
the whole stack (rate budget, retries, breaker, cache, credential rules), fetched only when the iteration reaches it.

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

`paginate` sends GETs, so it refuses body options (`json`, `data`, `content`, `files`) with `ValueError`. Its `params=`
are the first page's query, and a pager's parameter is added to them. `expect=` may be an `Expect`: a page can be a
declared answer like any other.

## Strategies

| Strategy | The next page is | Last page |
| --- | --- | --- |
| `Cursor(items=, next=, param=)` | the same request with `param` set to the cursor `next(page)` returns | `next` returns `None` or `""`, or a page with no items |
| `Offset(items=, limit=, param="offset", start=0)` | the same request with `param` advanced by the items this page held | a page with fewer than `limit` items |
| `LinkHeader(items=)` | the target of the response's `Link: <...>; rel="next"` (RFC 8288) | no `rel="next"` link, or a page with no items |
| `NextUrl(items=, next=)` | the URL `next(page)` returns, absolute or relative | `next` returns `None` or `""`, or a page with no items |

A page with no items ends a `Cursor`, `LinkHeader` or `NextUrl` walk even when it links to another: a link that
advances while returning nothing makes no progress, and following it spends the rate budget for nothing. For an
endpoint that sends empty pages mid-walk, pass `stop_on_empty=False`.

`Offset` sends only the offset; the page size goes in `params=`, under whatever name the endpoint uses (`limit`,
`per_page`, `page_size`). A next URL from the wire (`LinkHeader`, `NextUrl`) is resolved against the URL that answered
and sent as an absolute URL, under the usual rules: credentials go only to [trusted origins](security.md), and the SSRF
guard vets it when it is on.

Each strategy is a frozen value: build it once, next to the client, as with readers.

## A pager of your own

A scheme the four do not cover implements the `Pager[P, I]` protocol:

```python
import httpx2

from headgate import PageRequest


class PageNumbers:
    """Pages addressed by number, ending at the page count the body reports."""

    def first(self, request: PageRequest) -> PageRequest:
        return request.with_param("page", 1)

    def page_items(self, page: NumberedPage) -> list[Item]:
        return page.items

    def next_page(self, page: NumberedPage, request: PageRequest, response: httpx2.Response) -> PageRequest | None:
        return request.with_param("page", page.number + 1) if page.number < page.total_pages else None
```

`PageRequest(url, params)` is the GET for one page; `with_param(name, value)` returns it with one query parameter set.
`next_page` sees the page, the request that fetched it and the response (for its headers).
