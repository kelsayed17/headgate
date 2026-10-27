<p align="center">
  <img src="https://raw.githubusercontent.com/kelsayed17/headgate/main/docs/assets/logo.png" alt="headgate logo: a gate metering a stream of requests" width="160">
</p>

<h1 align="center">headgate</h1>

<p align="center">
  <strong>The Swiss Army knife for HTTP API clients in Python.</strong><br>
  Retries, rate limits, deadlines, circuit breaking, caching, pagination, streaming, auth and security,
  in one async base class built on <a href="https://pypi.org/project/httpx2/">httpx2</a>.
</p>

<p align="center">
  <a href="https://pypi.org/project/headgate/"><img src="https://img.shields.io/pypi/v/headgate" alt="PyPI"></a>
  <a href="https://pypi.org/project/headgate/"><img src="https://img.shields.io/pypi/pyversions/headgate" alt="Python versions"></a>
  <a href="https://github.com/kelsayed17/headgate/actions/workflows/ci.yml"><img src="https://github.com/kelsayed17/headgate/actions/workflows/ci.yml/badge.svg?branch=main" alt="CI"></a>
  <a href="https://github.com/kelsayed17/headgate/blob/main/CONTRIBUTING.md"><img src="https://img.shields.io/badge/coverage-100%25-brightgreen" alt="Coverage: 100%"></a>
  <a href="https://kelsayed17.github.io/headgate/"><img src="https://img.shields.io/badge/docs-kelsayed17.github.io%2Fheadgate-blue" alt="Docs"></a>
  <a href="https://github.com/kelsayed17/headgate/blob/main/LICENSE"><img src="https://img.shields.io/pypi/l/headgate" alt="Licence: MIT"></a>
</p>

<p align="center">
  <a href="https://kelsayed17.github.io/headgate/"><b>Documentation</b></a> ·
  <a href="https://kelsayed17.github.io/headgate/comparison/"><b>Compared with others</b></a> ·
  <a href="https://kelsayed17.github.io/headgate/standards/"><b>Standards</b></a> ·
  <a href="https://kelsayed17.github.io/headgate/security/"><b>Security</b></a> ·
  <a href="https://github.com/kelsayed17/headgate/blob/main/CHANGELOG.md"><b>Changelog</b></a>
</p>

## Why headgate

### 🧰 One class, not a stack

A production client usually grows an HTTP library, a retry library, a rate limiter, a cache, a circuit breaker, an
event-stream parser, a mocking library and the glue between them. headgate is all of them, built to cooperate: a cache
hit is never retried, a retry waits its rate turn, and one deadline bounds the lot.

### 🛡️ Secure by default

Credentials stay on your origin on every redirect and next-page link. An https-to-http redirect is refused. An SSRF
guard with DNS pinning is one option away. Secrets are redacted from every error, and nothing is read from the
environment.

### 📜 Built on the standards

17 RFCs and drafts, followed by default: `Retry-After`, the IETF `RateLimit` headers, Problem Details (RFC 9457), `Link`
pagination (RFC 8288), `Idempotency-Key`, `Deprecation` and `Sunset`, and more.

## A client in one screen

A client is a subclass. The configuration lives in `__init__`, and each endpoint method stays one line. This is part
of [`examples/github_client.py`](https://github.com/kelsayed17/headgate/blob/main/examples/github_client.py), a tested client for the GitHub REST API:

```python
from headgate import AsyncBaseClient, BearerAuth, Expect, HttpOptions, LinkHeader, Rate, RateBudget, nothing
from headgate.msgspec import decoder

# Readers and pagers are values: build them once, at import, where a bad declaration fails.
REPOSITORY = decoder(Repository)
REPOSITORY_OR_NONE = Expect(REPOSITORY).on(HTTPStatus.NOT_FOUND, nothing)  # a missing or hidden repository
ISSUES = decoder(list[Issue])
ISSUE_PAGES = LinkHeader(items=_issues)


class GitHub(AsyncBaseClient):
    """Read repositories and their open issues from the GitHub REST API."""

    def __init__(self, token: str, *, transport: httpx2.AsyncBaseTransport | None = None) -> None:
        """Authenticate with *token*; *transport* replaces the network, as a test does."""
        super().__init__(
            base_url=API,
            # Well under the 5,000 requests an hour a token gets, and never more than 50 at once.
            rate=RateBudget(Rate.per_second(10), max_in_flight=50),
            auth=BearerAuth(token),
            http=HttpOptions(
                transport=transport,
                headers={"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"},
            ),
        )

    async def repository(self, owner: str, name: str) -> Repository | None:
        """Return the repository *owner*/*name*, or ``None`` when it does not exist or the token cannot see it."""
        return await self.get(t"repos/{owner}/{name}", expect=REPOSITORY_OR_NONE)

    def open_issues(self, owner: str, name: str) -> Paginated[list[Issue], Issue]:
        """Return the open issues (and pull requests) of *owner*/*name*, 100 a page, fetched as they are iterated."""
        params = {"state": "open", "per_page": 100}
        return self.paginate(t"repos/{owner}/{name}/issues", expect=ISSUES, pages=ISSUE_PAGES, params=params)
```

Nothing in those methods mentions retries, pacing, GitHub's `X-RateLimit-*` headers, timeouts or where the token may
go, and nothing needs to. Those come from the base class.

## What it replaces

- 🔁 **Retries** (instead of tenacity, stamina or backoff):
  [`RetryPolicy`](https://kelsayed17.github.io/headgate/retries/), safe re-sends only, `Retry-After` obeyed, a budget
  across calls.
- 🚦 **Rate limits** (instead of aiolimiter or pyrate-limiter):
  [`RateBudget`](https://kelsayed17.github.io/headgate/rate-budgets/), paced, and paused when the server says so.
- ⏱️ **Deadlines** (instead of `asyncio.timeout` around each call, by hand): [one
  deadline](https://kelsayed17.github.io/headgate/deadlines/) over every attempt, wait and read.
- 🔌 **Circuit breaking** (instead of pybreaker or purgatory): [a breaker per
  origin](https://kelsayed17.github.io/headgate/breaker/), charged only for the host's own failures.
- 🗄️ **Caching** (instead of requests-cache, or hishel wired in by hand):
  [hishel](https://kelsayed17.github.io/headgate/caching/), partitioned by credential.
- 📄 **Pagination** (instead of a loop per endpoint): [`paginate()`](https://kelsayed17.github.io/headgate/pagination/),
  cursors, offsets, `Link` headers, next URLs.
- 📡 **Server-sent events** (instead of httpx-sse):
  [`stream().aiter_events()`](https://kelsayed17.github.io/headgate/streaming/#server-sent-events), with resume.
- 🧪 **Test doubles** (instead of respx or pytest-httpx):
  [`headgate.testing`](https://kelsayed17.github.io/headgate/testing/), a fake network and a clock that makes waits
  instant.

### Compared with HTTP clients

✅ built in · 🧩 with another library or your own code · ➖ not available

| | **headgate** | requests | httpx | aiohttp |
| --- | :---: | :---: | :---: | :---: |
| Async | ✅ | ➖ | ✅ | ✅ |
| Retries that wait for `Retry-After` | ✅ | 🧩 | 🧩 | 🧩 |
| One deadline over every retry | ✅ | 🧩 | 🧩 | 🧩 |
| Rate limiting that obeys the server | ✅ | 🧩 | 🧩 | 🧩 |
| Circuit breaker | ✅ | 🧩 | 🧩 | 🧩 |
| Every credential kept off other hosts | ✅ | 🧩 | 🧩 | 🧩 |
| SSRF guard with DNS pinning | ✅ | 🧩 | 🧩 | 🧩 |
| RFC 9457 problem details parsed | ✅ | ➖ | ➖ | ➖ |
| Test fakes | ✅ | 🧩 | 🧩 | 🧩 |

The [full comparison](https://kelsayed17.github.io/headgate/comparison/) covers about 30 libraries, and says when each is the better pick.

## Install

Python 3.14 or newer.

```bash
pip install 'headgate[msgspec]'    # or: uv add 'headgate[msgspec]'. headgate, and the msgspec codec
pip install 'headgate[pydantic]'   # headgate, and the pydantic codec
pip install 'headgate[curl]'       # + AsyncCurlTransport: a browser's TLS fingerprint, via curl_cffi
pip install 'headgate[otel]'       # + OpenTelemetryObserver (opentelemetry-api only)
pip install 'headgate[cache]'      # + what hishel's AsyncSqliteStorage needs (hishel[async]: anysqlite)
```

`pip install headgate` alone brings no serialization library: httpx2 and hishel only. Pick a codec as an extra, or
bring your own (see [codecs](https://kelsayed17.github.io/headgate/codecs/)).

<details>
<summary><b>Why Python 3.14, and why httpx2?</b></summary>

- **Why 3.14:** endpoint paths are [template strings](https://peps.python.org/pep-0750/) (`t"orders/{order_id}"`), so
  each value is escaped into its own path segment by default: `BTC/USD` becomes `BTC%2FUSD` instead of a new path.
  Template strings exist only from Python 3.14.
- **Why httpx2:** it is pydantic's maintained fork of httpx, and headgate is built and tested on it alone. Its API is
  httpx's, so moving a client over is mostly `import httpx2` where you had `import httpx`. Upstream `httpx` is not
  supported: passing one of its objects raises a `TypeError` naming the mistake. See
  [why httpx2](https://kelsayed17.github.io/headgate/comparison/#why-httpx2).

</details>

## Quickstart

A walkthrough of a whole client: typed responses, a declared 404, and cursor pagination. A path is a t-string, so
every value is escaped as one segment; `expect=` declares what the endpoint answers.

```python
from http import HTTPStatus
from typing import TYPE_CHECKING

import msgspec

from headgate import AsyncBaseClient, BearerAuth, Cursor, Expect, HttpOptions, Rate, nothing
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
            auth=BearerAuth(api_key),
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
request line, the status, the request id and every attempt. [`examples/`](https://github.com/kelsayed17/headgate/tree/main/examples/) has this client, the same client
in both codec flavors, and a fuller one ([`orders_client.py`](https://github.com/kelsayed17/headgate/blob/main/examples/orders_client.py)): a minted OAuth token, an
idempotent POST, a declared business refusal, `Link`-header pages and a streamed export.

## What every client gets

| Capability | What it does by default |
| --- | --- |
| [Declared responses](https://kelsayed17.github.io/headgate/responses/) | A reader for any 2xx, `Expect(...).on(status, reader)` for documented non-2xx answers; anything else raises |
| [Retries](https://kelsayed17.github.io/headgate/retries/) | 3 sends with jittered backoff; idempotent methods after a network failure or a 408, 425, 429, 500, 502, 503, 504; a POST only when provably unprocessed or keyed |
| [Retry budget and hedging](https://kelsayed17.github.io/headgate/retries/#the-retry-budget) | Retries capped at 20% of first attempts (plus 10 a second) per client; opt-in hedged sends for slow idempotent calls |
| [`Retry-After`](https://kelsayed17.github.io/headgate/retries/#retry-after) | Waited exactly up to 30 s, past that raised with `retry_after` set |
| [Rate budgets](https://kelsayed17.github.io/headgate/rate-budgets/) | GCRA pacing, shareable across clients and threads, per-path lanes, an optional in-flight cap |
| [Server feedback](https://kelsayed17.github.io/headgate/rate-budgets/#server-feedback) | 429/503 and `RateLimit` / `X-RateLimit-*` headers pause the whole budget; adaptive throttling (AIMD) |
| [Circuit breaker](https://kelsayed17.github.io/headgate/breaker/) | Per origin, opens after 5 consecutive origin failures, one probe after 60 s |
| [Deadlines and timeouts](https://kelsayed17.github.io/headgate/deadlines/) | 60 s for the whole call; each attempt's timeouts cut to what is left |
| [Credentials stay home](https://kelsayed17.github.io/headgate/security/) | Auth headers and params go only to `base_url`'s origin and `trusted_origins`, on every hop |
| [Redirect hardening](https://kelsayed17.github.io/headgate/security/#redirects) | No `https` to `http` downgrade, a hop limit, no re-sent one-shot body |
| [SSRF guard](https://kelsayed17.github.io/headgate/security/#the-ssrf-guard) | Opt-in: refuse private, loopback and other non-global addresses, pinned at connect time |
| [Response cap](https://kelsayed17.github.io/headgate/configuration/#httpoptions) | 32 MiB per buffered body, counted after decompression |
| [Caching](https://kelsayed17.github.io/headgate/caching/) | Opt-in hishel cache, partitioned by credential, never storing one; coalesced identical GETs; conditional reads |
| [Streaming and uploads](https://kelsayed17.github.io/headgate/streaming/) | `stream()` for bodies and server-sent events with resume; raw, streamed and multipart request bodies |
| [Pagination](https://kelsayed17.github.io/headgate/pagination/) | `paginate()` with `Cursor`, `Offset`, `LinkHeader` or `NextUrl`, lazy, loop-safe, bounded by pages or items |
| [Errors](https://kelsayed17.github.io/headgate/errors/) | One `ClientError` tree; method, URL, request id, attempts, `retryable`, `retry_after`, RFC 9457 problems |
| [Auth](https://kelsayed17.github.io/headgate/auth/) | Static headers, query keys, and a cached minted token with one re-mint on a 401 |
| [Observability](https://kelsayed17.github.io/headgate/observability/) | Typed events, OpenTelemetry spans and metrics, request ids, `Deprecation` / `Sunset` notices, health snapshots |
| [Testing kit](https://kelsayed17.github.io/headgate/testing/) | `FakeTransport`, `FaultyTransport`, `TestClock`, `isolated_breakers`, vcrpy cassette filters |
| [Per-call options](https://kelsayed17.github.io/headgate/configuration/#per-call-options) | `retry=`, `deadline=`, `timeout=`, `idempotency_key=`, `cache_ttl=`, and `with_options()` views |

How the pieces fit together, for contributors and the curious: [design](https://kelsayed17.github.io/headgate/design/).

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

`AsyncBaseClient(*, base_url, rate=UNLIMITED, auth=None, policy=TransportPolicy(), http=HttpOptions())`. `rate` is a
`Rate` (a budget of the client's own) or a `RateBudget` shared with other clients; without one the client paces nothing
itself but still waits out the pauses the server asks for. `TransportPolicy` says what the stack does to
every request, `HttpOptions` how requests are sent and read. Both are frozen and validate on construction.

<details>
<summary><b>A fuller client's configuration</b></summary>

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

</details>

| `TransportPolicy` | Default |
| --- | --- |
| `retry` | `RetryPolicy()`: 3 attempts |
| `deadline` | `60.0` seconds per call |
| `breaker` | `BreakerPolicy()`: 5 failures, 60 s |
| `rate_limits` | none |
| `cache` | none |
| `idempotency` | none |
| `routes` | none |

| `HttpOptions` | Default |
| --- | --- |
| `timeout` | `Timeouts()`: connect 10 s, read 30 s, write 10 s, pool 15 s |
| `headers` | none beyond `User-Agent` |
| `follow_redirects`, `max_redirects` | `True`, `5` |
| `max_response_bytes` | 32 MiB |
| `security` | `SecurityOptions()`: credentials to `base_url`'s origin only, SSRF guard off |
| `refusal_check` | none |
| `observers`, `middleware` | none |

Every option, its validation and the precedence of call, view, route and policy:
[configuration](https://kelsayed17.github.io/headgate/configuration/).

## Per-call options

Every verb (`get`, `post`, `put`, `patch`, `delete`, `head`) takes the URL, `expect=` (on `head`, `nothing` unless
given), and optionally `retry=False` or a `RetryPolicy`, `follow_redirects=`, `cache_ttl=`, `timeout=`, `deadline=`,
`idempotency_key=`, and the request options `params=`, `headers=`, `body=` (what a codec's `encoder` wrote), `json=`,
`data=`, `content=` and `files=`. `client.with_options(headers=..., timeout=..., deadline=..., retry=...)` returns a
view with other defaults that shares the pool, the budgets and the breakers; the client it came from owns the pool.

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
├── PageLimitError               a paginated walk spent its page budget with pages left
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

More in [errors](https://kelsayed17.github.io/headgate/errors/).

## Testing your client

`headgate.testing` tests a client through its real stack, with nothing private patched: a `FakeTransport` stands in
for the network, and a `TestClock` makes every wait instant.

```python
from quickstart import CatalogClient

from headgate.testing import FakeTransport, Respond, TestClock, isolated_breakers

GDP = {"id": "GDP", "title": "Gross domestic product", "values": [27.4, 28.1]}


async def test_find_series_reads_a_404_as_none() -> None:
    """Return ``None`` for the 404 the endpoint documents, signing the request with the key."""
    fake = FakeTransport().reply("GET", "/v1/series/RETIRED", 404)
    async with CatalogClient("test-key", transport=fake) as catalog:
        assert await catalog.find_series("RETIRED") is None
    assert fake.calls[0].headers["Authorization"] == "Bearer test-key"


async def test_a_503_is_retried_after_the_wait_it_asks_for() -> None:
    """Retry a 503 once its ``Retry-After`` has passed, on a test clock that makes the wait instant."""
    fake = FakeTransport().reply("GET", "/v1/series/GDP", Respond(503, retry_after=2.0), Respond(json=GDP))
    clock = TestClock()
    with clock.install(), isolated_breakers():  # the clients built in the block use them
        catalog = CatalogClient("test-key", transport=fake)
    async with catalog:
        series = await catalog.get_series("GDP")
    assert series.values == [27.4, 28.1]
    assert clock.sleeps == [2.0]
```

Fault schedules, cassettes and switching the policy off: [testing](https://kelsayed17.github.io/headgate/testing/).

## Codecs

The core imports no serialization library. `headgate.msgspec` (`headgate[msgspec]`) and `headgate.pydantic`
(`headgate[pydantic]`) are two codecs with the same shortcuts, `decode`, `decoder`, `encoder` and `to_builtins`, so
switching is changing one import. Both decode strictly (a field that changes type fails), ignore unknown fields, and
raise a mismatch as `DecodeError`, which the client turns into `ResponseParseError`.

Any other library plugs in as a `Codec`: an object naming its media type, the errors it raises, a decoder and an
encoder per type, and a description that never quotes the input. `decoder_for(codec, Type)` and
`encoder_for(codec, Type)` build a reader and a writer on it, and `headgate.testing.check_codec` holds it to the
promises the built-in codecs keep. See [codecs](https://kelsayed17.github.io/headgate/codecs/).

## Development

```bash
uv sync                                   # Python 3.14 and every dev dependency
uv run ruff check && uv run ruff format --check
uv run ty check
uv run python -m pytest --cov             # 100% line and branch coverage is required
uv run python -m pytest benchmarks        # the benchmarks, as CI runs them
uv run mutmut run                         # mutation testing; slow, run it before a release
uv run --only-group docs zensical serve   # preview the docs site at http://localhost:8000
```

Lint selects every ruff rule, ty treats every rule as an error, and suppression comments are not accepted. See
[CONTRIBUTING.md](https://github.com/kelsayed17/headgate/blob/main/CONTRIBUTING.md) for the rules a change must keep, [CODE_OF_CONDUCT.md](https://github.com/kelsayed17/headgate/blob/main/CODE_OF_CONDUCT.md) for how we
work together, and [SECURITY.md](https://github.com/kelsayed17/headgate/blob/main/SECURITY.md) to report a vulnerability privately. The changes in each release are in
[CHANGELOG.md](https://github.com/kelsayed17/headgate/blob/main/CHANGELOG.md).

## Licence

MIT. `src/headgate/_curl.py` is derived from [httpx-curl-cffi](https://github.com/vgavro/httpx-curl-cffi)
(BSD-3-Clause); its notice is kept in the file.
