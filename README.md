# headgate

An async HTTP client base for API clients, built on [`httpx2`](https://pypi.org/project/httpx2/).

**Requires Python 3.14 or newer and `httpx2`.** headgate does not support upstream `httpx`.

A headgate controls how much water gets through. This library does the same for requests: every client you build on it
gets retries, a rate budget, a per-host circuit breaker, a response size cap and an optional HTTP cache, all in one
place, so the client methods themselves stay one line each.

## Install

```bash
pip install headgate               # core: httpx2, hishel, aiolimiter, msgspec
pip install 'headgate[pydantic]'   # + the pydantic codec flavor
pip install 'headgate[curl]'       # + AsyncCurlTransport (browser TLS impersonation via curl_cffi)
```

## A client

```python
from headgate import AsyncBaseClient, QueryParamAuth, Rate
from headgate.msgspec import decoder
import msgspec


class Series(msgspec.Struct, frozen=True):
    series_id: str
    values: list[float]


class ExampleClient(AsyncBaseClient):
    def __init__(self, api_key: str) -> None:
        super().__init__(
            base_url="https://api.example.test/v1",
            rate=Rate.per_second(5),
            auth=QueryParamAuth({"api_key": api_key}),
        )

    async def get_series(self, series_id: str) -> Series:
        return await self.get("series", expect=decoder(Series), params={"id": series_id})
```

`examples/series_client.py` shows the same client in both codec flavors.

## Codec flavors

The core is codec-free. You choose how a body becomes a typed value by importing a flavor:

| Import | Decodes into | Notes |
|---|---|---|
| `headgate.msgspec` | `msgspec.Struct` | strict typing, fast; the default dependency |
| `headgate.pydantic` | `pydantic.BaseModel` | needs the `pydantic` extra |

Both expose `decode`, `decoder` and `to_builtins` with the same behavior, and both raise `headgate.errors.DecodeError`
on a body that does not match, which the client turns into `ResponseParseError`.

## The five contracts

1. **Declared answers, or raise.** Each call declares what it can get back with `expect=`: a reader for any 2xx, and,
   with `Expect(...).on(status, reader)`, the exact non-2xx statuses the provider documents as answers. Anything else
   raises a `ClientError` subtype carrying the status the wire sent. There is no raw mode.
2. **Transport only.** A client does I/O and decoding. Retry, rate limiting and the breaker live in the transport stack;
   business decisions belong to the caller.
3. **Wire types are data.** Responses decode into plain typed values with no behavior and no aliases invented by the client.
4. **No logging; typed events instead.** headgate emits no log records. A client reports its calls as frozen, typed
   events (`headgate.events`) to the observers installed with `HttpOptions(observers=...)`, carrying sanitized data
   only, and errors carry what a caller needs (client name, method, status, request id, attempt count, a bounded body
   snippet or RFC 9457 problem detail, a URL with secret query parameters redacted). The caller decides what to record.
5. **No environment reads.** Credentials are injected at construction. headgate never reads environment variables:
   the HTTP client and its default transport are built with `trust_env=False`.

## What the base client provides

- Retry with exponential backoff and `Retry-After`, per-call opt-out with `retry=False`.
- A token-bucket rate limit per client (`Rate.per_second`, `Rate.per_minute`) with per-path override rules.
- A per-host circuit breaker spanning the send and the body read.
- A response body cap measured on decompressed bytes.
- Auth helpers: `StaticHeaderAuth`, `QueryParamAuth`, `CachedTokenAuth`. A static credential header is withheld from any
  redirect hop that leaves the client's origin.
- An opt-in `HttpCache` (hishel), partitioned by credential, that degrades to the network when its storage fails.
- t-string URL paths (`self.get(t"series/{series_id}", ...)`) that escape each interpolated segment.

## Caching

```python
from hishel import AsyncSqliteStorage, FilterPolicy
from headgate import HttpCache, PathFragmentFilter, StatusCodeFilter, TransportPolicy

cache = HttpCache(
    policy=FilterPolicy(
        request_filters=[PathFragmentFilter(("/series",))],
        response_filters=[StatusCodeFilter(frozenset({200}))],
    ),
    storage=AsyncSqliteStorage(),  # yours to close; many clients may share it
    ttl=3600,
)
super().__init__(base_url=..., rate=..., auth=auth, policy=TransportPolicy(cache=cache))
```

- **Partitioned by credential.** An entry is visible only to requests made with the same credential: the partition is
  an HMAC-SHA256, under a per-process random key, of the auth's `credential_identity()` and of any other credential
  the request carries. Clients with different keys can share one storage. The auth must declare its credentials
  (`headgate._core.CredentialStamper`); an auth that does not is refused at construction unless you opt into
  `HttpCache(partition=Partition.SHARED)`, which is for public data only.
- **Credentials are never stored.** `Authorization`, `Proxy-Authorization`, `Cookie`, every header and query parameter
  the auth declares, well-known secret query parameters (`api_key`, `token`, ...) and URL userinfo are removed from
  what hishel stores, and sent to the network unchanged.
- **TTL, not revalidation.** A `FilterPolicy` cache serves an entry until its `ttl` (or a per-call `cache_ttl=`) ends,
  and never revalidates it with the origin. Credential-partitioned entries are not found again by another process.
- **Conditional reads** are the caller-driven alternative: keep the `Validator` and send it back.

```python
from headgate.cache import Fresh, NotModified, Validator, conditional

result = await self.get(t"docs/{doc_id}", expect=conditional(decoder(Doc), validator=held))
match result:
    case Fresh(value=doc, validator=held): ...  # new representation; keep its validator
    case NotModified(): ...                      # 304: what you hold is current
```

## Errors

`ClientError` is the root. `TransportError` (with `ClientTimeoutError`), `UpstreamStatusError` (with `RateLimitError`
and `AuthError`) and `ResponseParseError` let a boundary map failures to the right response.

## Development

```bash
uv sync
uv run python -m pytest
uv run ruff check . && uv run ty check
```

The test suite uses `pytest-httpx` through a small alias plugin (`headgate_pytest_alias.py`) so its `httpx_mock` fixture
patches `httpx2`.

## Licence

MIT. `src/headgate/_curl.py` is derived from `httpx-curl-cffi` (BSD-3-Clause); its notice is kept in the file.
