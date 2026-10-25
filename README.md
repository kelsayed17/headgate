# headgate

An async HTTP client base for API clients, built on [`httpx2`](https://pypi.org/project/httpx2/).

**Requires Python 3.14 or newer and `httpx2`.** headgate does not support upstream `httpx`.

A headgate controls how much water gets through. This library does the same for requests: every client you build on it
gets retries, a rate budget, a per-host circuit breaker, a response size cap and an optional HTTP cache, all in one
place, so the client methods themselves stay one line each.

## Install

```bash
pip install headgate               # core: httpx2, hishel, msgspec
pip install 'headgate[pydantic]'   # + the pydantic codec flavor
pip install 'headgate[curl]'       # + AsyncCurlTransport (browser TLS impersonation via curl_cffi)
pip install 'headgate[otel]'       # + OpenTelemetryObserver (opentelemetry-api only)
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
- A rate budget per client, or shared between clients, with per-path rules (see [Rate budgets](#rate-budgets)).
- A per-origin circuit breaker spanning the send and the body read (see [Circuit breaker](#circuit-breaker)).
- A response body cap measured on decompressed bytes.
- Auth helpers: `StaticHeaderAuth`, `QueryParamAuth`, `CachedTokenAuth`. A static credential header is withheld from any
  redirect hop that leaves the client's origin.
- An opt-in `HttpCache` (hishel), partitioned by credential, that degrades to the network when its storage fails.
- t-string URL paths (`self.get(t"series/{series_id}", ...)`) that escape each interpolated segment.

## Rate budgets

`rate=Rate.per_second(5)` gives a client a budget of its own. A `RateBudget` is the same budget as an object you can
share: pass one instance to several clients (or to several `RateRule`s) and they spend from it together, even across
event loops and threads.

```python
from headgate import AsyncBaseClient, Rate, RateBudget, RateRule, TransportPolicy

shared = RateBudget(Rate.per_minute(600), max_in_flight=8)  # one quota, two clients
search = AsyncBaseClient(base_url="https://api.example.test/search", rate=shared)
reports = AsyncBaseClient(
    base_url="https://api.example.test/reports",
    rate=shared,
    policy=TransportPolicy(rate_limits=(RateRule("/export", Rate.per_minute(6)),)),  # a slower lane of its own
)
```

- **Pacing**: up to `requests` back to back, then one every `period / requests` seconds, first come first served.
  Every attempt spends its own turn; a cache hit spends none.
- **`max_in_flight`**: at most that many requests open at once, each counted until its response body is closed.
- **Server feedback**: a 429/503 `Retry-After`, or rate-limit headers saying the quota is spent (`RateLimit` /
  `RateLimit-Policy` from the IETF draft, or `X-RateLimit-Remaining` / `X-RateLimit-Reset`), pauses the whole budget
  until the reset, so concurrent calls queue instead of collecting refusals. A call whose deadline ends inside the
  pause raises `RateLimitError` at once, with `retry_after` set.
- **Adaptive throttling** (on by default, `adaptive=None` turns it off): each pushback halves the pacing rate, down to
  `Adaptive.min_rate` of it; successes restore it gradually. Waits are reported as `RateLimited` events.

## Circuit breaker

Each origin (`scheme://host:port`) gets a breaker shared by every client whose `BreakerPolicy` names the same
registry and thresholds; a client asking for other thresholds gets its own breaker for that origin, never one fixed by
whichever client came first. `failure_threshold` consecutive failures (default 5) open it; after `recovery_timeout`
seconds (default 60) one probe call is let through, and its outcome closes or re-opens it. A refused call raises
`CircuitOpenError` with `retry_after`, and a retry sequence stops as soon as the breaker opens.

Only what the origin said counts: a 5xx that survived retries, or a network failure. A 429, a cache hit or a cached
5xx, a cancelled call, an auth mint failure, an oversized body and failures of our own side (a refused header,
`PoolTimeout`) record nothing. Every transition is a `BreakerChanged` event, and `headgate.health.breaker_states()`
snapshots them. The registry (`BreakerRegistry(max_hosts=1024)`) is thread-safe and drops its least recently used
closed breaker when full; `TransportPolicy(breaker=None)` turns the breaker off.

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
    case Fresh(value=doc, validator=held):
        ...  # new representation; keep its validator
    case NotModified():
        ...  # 304: what you hold is current

## Streaming

`stream()` opens a 2xx response under `async with` and reads it as it arrives, never buffered and never capped. The
deadline bounds opening the stream; a failure mid-body raises `TransportError` / `ClientTimeoutError` from the loop.

```python
async with client.stream("GET", "exports/2026.csv") as stream:
    async for chunk in stream.aiter_bytes():
        sink.write(chunk)

async with client.stream("GET", "events", headers={"Accept": "text/event-stream"}) as stream:
    async for event in stream.aiter_events():  # parsed as each event arrives: event, data, id, retry
        handle(event)
# Resume later after the last event seen:
async with client.stream("GET", "events", last_event_id=stream.last_event_id) as stream: ...
```

## Uploads

`content=` sends raw bytes, a one-shot async iterable (the call is then never re-sent), or a factory returning one,
called once per attempt so the call stays retryable. `files=` sends a multipart body, with `data=` as its form fields.

```python
await client.put("blobs/1", expect=nothing, content=lambda: read_chunks(path))
await client.post("reports", expect=nothing, files={"report": ("q3.csv", fh, "text/csv")}, data={"kind": "q"})
```

## Pagination

`paginate()` follows a paginated endpoint lazily, one GET through the full stack per page: `async for` yields items,
`.pages()` yields pages. The strategies are `Cursor(items=, next=, param=)`, `Offset(items=, limit=, param=,
start=)`, `LinkHeader(items=)` (RFC 8288 `rel="next"`) and `NextUrl(items=, next=)`; a next URL from the wire is sent
absolute, under the usual credential rules, and a page that leads back to an earlier one raises instead of looping.

```python
def items_of(page: Page) -> list[Item]:
    return page.data


def cursor_of(page: Page) -> str | None:
    return page.next_cursor


pages = Cursor(items=items_of, next=cursor_of, param="after")
async for item in client.paginate("items", expect=decoder(Page), pages=pages):  # item: Item
    ...
```

## Per-call defaults

`client.with_options(headers=..., timeout=..., deadline=..., retry=...)` returns a view of the client with other
defaults for its calls. It shares the connection pool, rate budget, retry state and circuit breakers; the client it
came from owns the pool, so closing a view closes nothing. `Expect(reader, accept="application/json")` sends that
`Accept` header and raises `ResponseParseError` (with the status and `Content-Type`) for a body of another media type
before the reader runs; parameters are ignored and a `+json` suffix counts as JSON.

## Observability

- **Events.** Install observers with `HttpOptions(observers=(...))`: each call reports `CallStarted`, one
  `AttemptFinished` per send and `CallFinished`, plus `Deprecated` when the response carries a `Deprecation`
  (RFC 9745) or `Sunset` (RFC 8594) header (dates parsed, `rel="deprecation"` / `rel="sunset"` links resolved). Events
  carry sanitized data only. See `headgate.events`.
- **OpenTelemetry** (`pip install 'headgate[otel]'`): `http = OpenTelemetryObserver().install(HttpOptions(...))` gives
  every call one CLIENT span covering all its attempts and the body read, with the HTTP semantic-convention attributes
  (the t-string route as `url.template`, re-sends as `http.request.resend_count`), the `http.client.request.duration`
  histogram, and a W3C `traceparent` on every attempt. It works with any OpenTelemetry SDK, Logfire or Sentry's
  OpenTelemetry integration, and nests the per-attempt spans of `HTTPX2ClientInstrumentor` under its own; see
  `headgate.otel` for how to avoid recording the duration twice.
- **Request ids.** Every `ClientError` carries `request_id`, read from the first of `HttpOptions.request_id_headers`
  the response has (`x-request-id`, `request-id`, AWS's, Azure's, Cloudflare's by default).

## Testing your client

`headgate.testing` tests a client without patching anything private:

```python
from headgate import AsyncBaseClient, HttpOptions, Rate, as_bytes
from headgate.testing import FakeTransport, TestClock, isolated_breakers

fake = FakeTransport().route("GET", "/v1/series", 503, httpx2.Response(200, content=b"ok"))
clock = TestClock()
with clock.install(), isolated_breakers():  # the clients built in the block use them
    client = AsyncBaseClient(
        base_url="https://api.example.test/v1", rate=Rate.per_second(5), http=HttpOptions(transport=fake)
    )
assert await client.get("series", expect=as_bytes) == b"ok"
assert len(fake.calls) == 2 and clock.sleeps  # the retry waited, on the test clock
```

`FakeTransport` answers from a method-and-path route table and logs every request; `TestClock` is a manual clock whose
sleep advances it; `instant_retries()` retries without waiting; `isolated_breakers()` gives the clients built in its
block a fresh breaker registry. `CassetteFilter` builds a vcrpy / pytest-recording `vcr_config` that filters
headgate's credential headers and secret query parameters and, installed as middleware, skips token-mint requests.

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
