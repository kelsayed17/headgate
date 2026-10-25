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

- Retries that re-send only when it is safe, within a budget and the call's deadline (see [Retries](#retries)).
- A token-bucket rate limit per client (`Rate.per_second`, `Rate.per_minute`) with per-path override rules.
- A per-host circuit breaker spanning the send and the body read.
- A response body cap measured on decompressed bytes.
- Auth helpers: `StaticHeaderAuth`, `QueryParamAuth`, `CachedTokenAuth`. A static credential header is withheld from any
  redirect hop that leaves the client's origin.
- An opt-in `HttpCache` (hishel) that degrades to the network when its storage fails.
- t-string URL paths (`self.get(t"series/{series_id}", ...)`) that escape each interpolated segment.

## Retries

`TransportPolicy(retry=RetryPolicy(...))` sets the policy once on the client; a call overrides it with
`retry=RetryPolicy(...)` or turns it off with `retry=False`.

- **What is re-sent.** Up to `attempts=3` sends, on a network failure or a status in `statuses` (408, 425, 429, 500,
  502, 503, 504), with full-jitter exponential backoff between them. An idempotent method (`methods`: GET, HEAD,
  OPTIONS, PUT, DELETE) is re-sent after any of these. A POST or PATCH is re-sent only when nothing of it was written
  (each attempt's connection and written bytes are tracked), when the server proved it did not process it (425 Too
  Early, an HTTP/2 `REFUSED_STREAM`), or when it carries an idempotency key. A server's `x-should-retry: true|false`
  decides when it sends one. Local mistakes (an unencodable header, an unsupported scheme) are never re-sent.
- **`Retry-After`.** A 429's or 503's `Retry-After` (seconds, fractional seconds or an HTTP-date) or `retry-after-ms` is
  waited exactly when it is at most `max_retry_after` (30 s) and fits in the deadline. Otherwise the call stops at once
  and raises `RateLimitError` (429) or `UpstreamStatusError` with `retry_after` set, so the caller can reschedule.
- **Budget.** Retries may add at most `RetryBudget(ratio=0.2, min_per_second=10, window=10.0)`: 20% of first attempts
  over a sliding 10 s window, plus a floor of 10 per second. The budget is shared by every call of a client, so an
  outage cannot multiply its traffic; a call that finds it spent raises with the note `retry budget exhausted`.
- **Deadline and breaker.** Each attempt's timeouts are cut to the time left before the call's deadline, and no retry
  or wait starts with less than `min_attempt_time` (0.05 s) left. A retry sequence stops as soon as the host's breaker
  opens, and counts once toward it.
- **Hedging (opt-in).** `RetryPolicy(hedge=Hedge(after=0.5, max_extra=1))` sends a second copy of an idempotent request
  that has not answered after `after` seconds; the first answer wins and the other send is cancelled. Hedges count as
  attempts and spend the budget.
- **History.** The error a call raises carries `attempts`, `retryable`, `retry_after`, and one note per attempt, such
  as `attempt 2/3: 503 Service Unavailable (Retry-After 2.0s) after 0.41s`. Notes name statuses and exception types
  only, never a header or a URL's secrets.

To make a POST safe to retry, give it an idempotency key the upstream deduplicates on: stamp one on every POST and
PATCH with `TransportPolicy(idempotency=IdempotencyKeys())`, or pass `idempotency_key=` on the call. Every attempt of
the call carries the same key.

`headgate.testing.FaultyTransport` plays a schedule of faults (`ConnectFail()`, `Respond(503, retry_after=1)`,
`DropMidBody(after_bytes=100)`, `Slow(seconds=2)`, `RefusedStream()`, ...) and records what each attempt sent, for
testing a client's retry behaviour.

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
