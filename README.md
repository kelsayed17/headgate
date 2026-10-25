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
- A token-bucket rate limit per client (`Rate.per_second`, `Rate.per_minute`) with per-path override rules.
- A per-host circuit breaker spanning the send and the body read.
- A response body cap measured on decompressed bytes.
- Auth helpers: `StaticHeaderAuth`, `QueryParamAuth`, `CachedTokenAuth`. A static credential header is withheld from any
  redirect hop that leaves the client's origin.
- An opt-in `HttpCache` (hishel) that degrades to the network when its storage fails.
- t-string URL paths (`self.get(t"series/{series_id}", ...)`) that escape each interpolated segment.

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
