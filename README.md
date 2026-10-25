# headgate

**An async API-client base for Python, built on [httpx2](https://pypi.org/project/httpx2/).** Subclass
`AsyncBaseClient`, declare what each endpoint answers, and every client you build gets the same production behaviour:
retries that re-send only when it is safe, a shared rate budget that listens to the server, a per-origin circuit
breaker, whole-call deadlines, credentials that never leave your origin, an opt-in cache that never stores them, typed
events and OpenTelemetry, and errors that say exactly what happened. Each endpoint method stays one line.

A headgate controls how much water gets through a channel. This library does the same for requests, in one place,
so that no client has to get it right on its own.

## Requirements and install

Python 3.14 or newer (endpoint paths are t-strings) and `httpx2`, pydantic's fork of httpx. Upstream `httpx` is not
supported: passing one of its objects raises a `TypeError` naming the mistake.

```bash
pip install headgate               # or: uv add headgate. Brings httpx2[http2], hishel and msgspec
pip install 'headgate[pydantic]'   # + the pydantic codec flavor
pip install 'headgate[curl]'       # + AsyncCurlTransport: a browser's TLS fingerprint, via curl_cffi
pip install 'headgate[otel]'       # + OpenTelemetryObserver (opentelemetry-api only)
pip install 'headgate[cache]'      # + what hishel's AsyncSqliteStorage needs (hishel[async]: anysqlite)
```

## Quickstart

A client is a subclass that fixes its configuration in `super().__init__()` and exposes one method per endpoint. A
path is a t-string, so every value is escaped as one segment; `expect=` declares what the endpoint answers.

```python
from http import HTTPStatus
from typing import TYPE_CHECKING

import msgspec

from headgate import AsyncBaseClient, Cursor, Expect, HttpOptions, Rate, StaticHeaderAuth, nothing
from headgate.msgspec import decoder

if TYPE_CHECKING:
    import httpx2

    from headgate import Paginated


class Series(msgspec.Struct, frozen=True):
    """A data series: its id, title and observations."""

    id: str
    title: str
    values: list[float]


class Release(msgspec.Struct, frozen=True):
    """A scheduled data release."""

    id: int
    name: str


class ReleasePage(msgspec.Struct, frozen=True):
    """One page of releases, and the cursor of the next page (``None`` on the last)."""

    releases: list[Release]
    next_cursor: str | None = None


def releases_on(page: ReleasePage) -> list[Release]:
    """Return the releases on *page*."""
    return page.releases


def cursor_after(page: ReleasePage) -> str | None:
    """Return the cursor of the page after *page*."""
    return page.next_cursor


# Readers and pagers are values: build them once, at import, where a bad declaration fails.
SERIES = decoder(Series)
SERIES_OR_NONE = Expect(SERIES).on(HTTPStatus.NOT_FOUND, nothing)  # the API documents 404 as "no such series"
RELEASE_PAGES = Cursor(items=releases_on, next=cursor_after, param="cursor")


class CatalogClient(AsyncBaseClient):
    """The catalog API: one method per endpoint, each one line."""

    def __init__(self, api_key: str, *, transport: httpx2.AsyncBaseTransport | None = None) -> None:
        """Authenticate with *api_key*; *transport* replaces the network, as a test does."""
        super().__init__(
            base_url="https://api.catalog.example/v1",
            rate=Rate.per_second(10),
            auth=StaticHeaderAuth({"Authorization": f"Bearer {api_key}"}),
            http=HttpOptions(transport=transport),
        )

    async def get_series(self, series_id: str) -> Series:
        """Return the series *series_id*; a 404, undeclared here, raises ``UpstreamStatusError``."""
        return await self.get(t"series/{series_id}", expect=SERIES)

    async def find_series(self, series_id: str) -> Series | None:
        """Return the series *series_id*, or ``None`` when the API answers 404."""
        return await self.get(t"series/{series_id}", expect=SERIES_OR_NONE)

    def releases(self) -> Paginated[ReleasePage, Release]:
        """Return every release, fetched a page at a time as the iteration reaches it."""
        return self.paginate("releases", expect=decoder(ReleasePage), pages=RELEASE_PAGES)
```

Use it with `async with`, which closes the connection pool on the way out:

```python
async def main(api_key: str) -> tuple[float, Series | None, list[str]]:
    """Read one series, look up one that may not exist, and list every release."""
    async with CatalogClient(api_key) as catalog:
        gdp = await catalog.get_series("GDP")
        retired = await catalog.find_series("RETIRED")  # None: the API answered 404
        names = [release.name async for release in catalog.releases()]
    return gdp.values[-1], retired, names
```

Nothing in those methods mentions retries, rate limits, timeouts or credentials, and nothing needs to:
`get_series("GDP")` is paced by the client's budget, bounded by a 60-second deadline, retried on a 503 or a dropped
connection, refused fast while the origin's breaker is open, and fails with an error that names the client, the
request line, the status, the request id and every attempt. [`examples/`](examples/) has this client, the same client
in both codec flavors, and a fuller one ([`orders_client.py`](examples/orders_client.py)): a minted OAuth token, an
idempotent POST, a declared business refusal, `Link`-header pages and a streamed export.

## What every client gets

| Capability | What it does by default | Guide |
| --- | --- | --- |
| Declared responses | A reader for any 2xx, `Expect(...).on(status, reader)` for documented non-2xx answers; anything else raises | [responses](docs/responses.md) |
| Retries | 3 sends with jittered backoff; idempotent methods after a network failure or a 408, 425, 429, 500, 502, 503, 504; a POST only when provably unprocessed or keyed | [retries](docs/retries.md) |
| Retry budget and hedging | Retries capped at 20% of first attempts (plus 10 a second) per client; opt-in hedged sends for slow idempotent calls | [retries](docs/retries.md#the-retry-budget) |
| `Retry-After` | Waited exactly up to 30 s, past that raised with `retry_after` set | [retries](docs/retries.md#retry-after) |
| Rate budgets | GCRA pacing, shareable across clients and threads, per-path lanes, an optional in-flight cap | [rate budgets](docs/rate-budgets.md) |
| Server feedback | 429/503 and `RateLimit` / `X-RateLimit-*` headers pause the whole budget; adaptive throttling (AIMD) | [rate budgets](docs/rate-budgets.md#server-feedback) |
| Circuit breaker | Per origin, opens after 5 consecutive origin failures, one probe after 60 s | [breaker](docs/breaker.md) |
| Deadlines and timeouts | 60 s for the whole call; each attempt's timeouts cut to what is left | [deadlines](docs/deadlines.md) |
| Credentials stay home | Auth headers and params go only to `base_url`'s origin and `trusted_origins`, on every hop | [security](docs/security.md) |
| Redirect hardening | No `https` to `http` downgrade, a hop limit, no re-sent one-shot body | [security](docs/security.md#redirects) |
| SSRF guard | Opt-in: refuse private, loopback and other non-global addresses, pinned at connect time | [security](docs/security.md#the-ssrf-guard) |
| Response cap | 32 MiB per buffered body, counted after decompression | [configuration](docs/configuration.md#httpoptions) |
| Caching | Opt-in hishel cache, partitioned by credential, never storing one; coalesced identical GETs; conditional reads | [caching](docs/caching.md) |
| Streaming and uploads | `stream()` for bodies and server-sent events with resume; raw, streamed and multipart request bodies | [streaming](docs/streaming.md) |
| Pagination | `paginate()` with `Cursor`, `Offset`, `LinkHeader` or `NextUrl`, lazy, loop-safe | [pagination](docs/pagination.md) |
| Errors | One `ClientError` tree; method, URL, request id, attempts, `retryable`, `retry_after`, RFC 9457 problems | [errors](docs/errors.md) |
| Auth | Static headers, query keys, and a cached minted token with one re-mint on a 401 | [auth](docs/auth.md) |
| Observability | Typed events, OpenTelemetry spans and metrics, request ids, `Deprecation` / `Sunset` notices, health snapshots | [observability](docs/observability.md) |
| Testing kit | `FakeTransport`, `FaultyTransport`, `TestClock`, `isolated_breakers`, vcrpy cassette filters | [testing](docs/testing.md) |
| Per-call options | `retry=`, `deadline=`, `timeout=`, `idempotency_key=`, `cache_ttl=`, and `with_options()` views | [configuration](docs/configuration.md#per-call-options) |

How the pieces fit together, for contributors and the curious: [design](docs/design.md).

## The contracts

1. **Declared answers, or raise.** A call returns what its reader read from a 2xx or from a status it declared, and
   raises a `ClientError` for anything else. There is no raw mode and no `None` for "it failed".
2. **Transport only.** A client does I/O and decoding. Retries, pacing and the breaker live in the transport stack;
   business decisions belong to the caller.
3. **Wire types are data.** Responses decode into plain typed values (msgspec structs or pydantic models) with no
   behaviour.
4. **No logging; typed events instead.** headgate writes no log records. It reports to the observers you install,
   with sanitized data only, and its errors carry what you need to diagnose a failure.
5. **No environment reads.** Credentials are passed in. No proxy variable, `.netrc` or `SSL_CERT_FILE` is read: the
   HTTP client and its transport are built with `trust_env=False`.

## Configuration

`AsyncBaseClient(*, base_url, rate, auth=None, policy=TransportPolicy(), http=HttpOptions())`. `rate` is a `Rate` (a
budget of the client's own) or a `RateBudget` shared with other clients. `TransportPolicy` says what the stack does to
every request, `HttpOptions` how requests are sent and read. Both are frozen and validate on construction.

```python
class OrdersClient(AsyncBaseClient):
    """The shop's order API."""

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        *,
        observers: tuple[Observer, ...] = (),
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        """Mint tokens with *client_id* and *client_secret*; report to *observers*; *transport* replaces the network."""
        super().__init__(
            base_url="https://api.shop.example/v2",
            rate=Rate.per_second(20),
            auth=ShopTokenAuth(client_id, client_secret),
            policy=TransportPolicy(
                retry=RetryPolicy(attempts=4),  # four sends at most, the first included
                deadline=30.0,  # each call, every attempt and wait included
                idempotency=IdempotencyKeys(),  # every POST and PATCH carries a key, so it is retried safely
                rate_limits=(RateRule("/exports", Rate.per_minute(6)),),  # a slower lane for the export endpoint
                routes=(Route("/exports", deadline=300.0),),  # and a longer deadline
            ),
            http=HttpOptions(
                transport=transport,
                timeout=Timeouts(read=20.0),
                user_agent="shop-sync/1.4",
                observers=observers,
            ),
        )
```

| `TransportPolicy` | Default | | `HttpOptions` | Default |
| --- | --- | --- | --- | --- |
| `retry` | `RetryPolicy()`: 3 attempts | | `timeout` | `Timeouts(connect=10, read=30, write=10, pool=15)` |
| `deadline` | `60.0` seconds per call | | `headers` | none beyond `User-Agent` |
| `breaker` | `BreakerPolicy()`: 5 failures, 60 s | | `follow_redirects`, `max_redirects` | `True`, `5` |
| `rate_limits` | none | | `max_response_bytes` | 32 MiB |
| `cache` | none | | `trusted_origins` | `base_url`'s origin only |
| `idempotency` | none | | `allow_private_hosts` | `True` (SSRF guard off) |
| `routes` | none | | `observers`, `middleware` | none |

Every option, its validation and the precedence of call, view, route and policy:
[configuration](docs/configuration.md).

## Per-call options

Every verb (`get`, `post`, `put`, `patch`, `delete`, `head`) takes the URL, `expect=`, and optionally `retry=False` or
a `RetryPolicy`, `follow_redirects=`, `cache_ttl=`, `timeout=`, `deadline=`, `idempotency_key=`, and the request
options `params=`, `headers=`, `json=`, `data=`, `content=` and `files=`. `client.with_options(headers=...,
timeout=..., deadline=..., retry=...)` returns a view with other defaults that shares the pool, the budgets and the
breakers; the client it came from owns the pool.

## Errors

Every failure is a `ClientError`, so an application boundary can map the tree onto its own answers:

```text
ClientError                      also: an unparseable URL, a pagination loop
├── TransportError               a network failure that outlasted retries
│   ├── ClientTimeoutError       a timeout, or the call's deadline
│   └── CircuitOpenError         the origin's breaker refused the call unsent
├── UpstreamStatusError          an undeclared status; a 5xx that outlasted retries; a refusal in a body
│   ├── AuthError                401 or 403: our credential was refused
│   └── RateLimitError           429, a quota refusal, or a server pause past the deadline
├── ResponseParseError           a 2xx body the reader could not read
├── ResponseTooLargeError        a body past max_response_bytes
├── ForbiddenDestinationError    a hop the SSRF guard refused
└── ClientConfigurationError     our own setup is wrong
```

Every error carries `method`, `url` (credentials redacted), `request_id`, `attempts`, `retryable`, `retry_after` and a
note per attempt (`attempt 2/3: 503 Service Unavailable (Retry-After 2.0s) after 0.41s`). An `UpstreamStatusError`
adds `status_code`, the provider's `code` and its RFC 9457 `problem`. All of them pickle.

```python
def gateway_status(error: ClientError) -> HTTPStatus:
    """Return the status an API gateway answers with when a call to an upstream failed with *error*."""
    match error:
        case ClientTimeoutError():
            return HTTPStatus.GATEWAY_TIMEOUT  # the upstream was too slow, or the call's deadline passed
        case ClientConfigurationError():
            return HTTPStatus.SERVICE_UNAVAILABLE  # our own setup is wrong; an operator must act
        case _:
            return HTTPStatus.BAD_GATEWAY  # the upstream failed, refused us, or sent something unreadable
```

More in [errors](docs/errors.md).

## Testing your client

`headgate.testing` tests a client through its real stack, with nothing private patched: a `FakeTransport` stands in
for the network, and a `TestClock` makes every wait instant.

```python
import httpx2
from quickstart import CatalogClient

from headgate.testing import FakeTransport, TestClock, isolated_breakers

GDP = {"id": "GDP", "title": "Gross domestic product", "values": [27.4, 28.1]}


async def test_find_series_reads_a_404_as_none() -> None:
    """Return ``None`` for the 404 the endpoint documents, signing the request with the key."""
    fake = FakeTransport().route("GET", "/v1/series/RETIRED", 404)
    async with CatalogClient("test-key", transport=fake) as catalog:
        assert await catalog.find_series("RETIRED") is None
    assert fake.calls[0].headers["Authorization"] == "Bearer test-key"


async def test_a_503_is_retried_after_the_wait_it_asks_for() -> None:
    """Retry a 503 once its ``Retry-After`` has passed, on a test clock that makes the wait instant."""
    fake = FakeTransport().route(
        "GET",
        "/v1/series/GDP",
        httpx2.Response(503, headers={"Retry-After": "2"}),
        httpx2.Response(200, json=GDP),
    )
    clock = TestClock()
    with clock.install(), isolated_breakers():  # the clients built in the block use them
        catalog = CatalogClient("test-key", transport=fake)
    async with catalog:
        series = await catalog.get_series("GDP")
    assert series.values == [27.4, 28.1]
    assert clock.sleeps == [2.0]
```

Fault schedules, cassettes and switching the policy off: [testing](docs/testing.md).

## Codec flavors

The core imports no codec. A client picks one by import, and both flavors expose `decode`, `decoder` and
`to_builtins` with the same behaviour: strict typing (a field that changes type fails), unknown fields ignored, and a
mismatch raised as `DecodeError`, which the client turns into `ResponseParseError`.

| Import | Decodes into | Install |
| --- | --- | --- |
| `headgate.msgspec` | `msgspec.Struct` and any msgspec type | core |
| `headgate.pydantic` | `pydantic.BaseModel` and any pydantic type | `headgate[pydantic]` |

## Development

```bash
uv sync                                   # Python 3.14 and every dev dependency
uv run ruff check && uv run ruff format --check
uv run ty check
uv run python -m pytest --cov             # 100% line and branch coverage is required
uv run python -m pytest benchmarks        # the benchmarks, as CI runs them
uv run mutmut run                         # mutation testing; slow, run it before a release
```

Lint selects every ruff rule, ty treats every rule as an error, and suppression comments are not accepted. See
[CONTRIBUTING.md](CONTRIBUTING.md) for the rules a change must keep, [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) for how we
work together, and [SECURITY.md](SECURITY.md) to report a vulnerability privately. The changes in each release are in
[CHANGELOG.md](CHANGELOG.md).

## Licence

MIT. `src/headgate/_curl.py` is derived from [httpx-curl-cffi](https://github.com/vgavro/httpx-curl-cffi)
(BSD-3-Clause); its notice is kept in the file.
