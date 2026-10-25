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
- Auth helpers: `StaticHeaderAuth`, `QueryParamAuth`, `CachedTokenAuth`, whose credentials stay on your origin (below).
- An opt-in `HttpCache` (hishel) that degrades to the network when its storage fails.
- t-string URL paths (`self.get(t"series/{series_id}", ...)`) that escape each interpolated segment.

## Credentials stay on your origin

Every auth headgate ships declares the headers and query parameters it stamps. They are sent only to `base_url`'s
origin and the origins in `HttpOptions(trusted_origins=...)`, on every hop: the first request (an absolute cursor URL
from a response body), each redirect, and each redirect a token mint is answered with. A request to any other origin
still goes out, unsigned, so a signed download URL on a CDN works. `Authorization`, `Proxy-Authorization` and (without
`cookies=True`) `Cookie` are withheld from untrusted origins whatever the auth, a third-party one included. A redirect
from `https` to `http` is never followed: it raises as the 3xx it is.

A `CachedTokenAuth` subclass whose `_attach` sets another header declares it:

```python
class CrumbAuth(CachedTokenAuth):
    token_headers = frozenset({"X-Crumb"})

    def _attach(self, request: httpx2.Request, token: str) -> None:
        request.headers["X-Crumb"] = token
```

## SSRF protection

A client that fetches URLs it did not write (webhooks, user-supplied links, cursors) sets
`HttpOptions(allow_private_hosts=False)`. Every hop's host is resolved and refused, as `ForbiddenDestinationError`,
when any address it resolves to is loopback, private, link-local, multicast, reserved or unspecified, IPv4-mapped and
NAT64 forms included. The default transport connects only to the address it checked, so a DNS answer that changes
between check and connect cannot redirect it; TLS still verifies the host name. A custom transport (curl included) is
checked on every hop but resolves names itself, so it is not pinned. The guard cannot be combined with `proxy=`, and
`HttpOptions(resolver=...)` replaces the event loop's `getaddrinfo`.

## Errors

`ClientError` is the root. `TransportError` (with `ClientTimeoutError`), `UpstreamStatusError` (with `RateLimitError`
and `AuthError`), `ResponseParseError` and `ForbiddenDestinationError` let a boundary map failures to the right
response.

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
