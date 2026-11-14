# Configuration

```text
AsyncBaseClient(
    *,
    base_url: str,
    rate: Rate | RateBudget | None = None,
    auth: httpx2.Auth | None = None,
    policy: TransportPolicy = TransportPolicy(),
    http: HttpOptions = HttpOptions(),
    observers: Sequence[Observer] = (),
    middleware: Sequence[Middleware] = (),
)
```

A client class fixes these in `super().__init__()` and exposes only what its callers should choose (a key, a region,
observers, a transport for tests). Every option object is a frozen, keyword-only dataclass that validates when it is
built, so a bad value fails where it is written, and freezes the collections it is given. Change one with
`dataclasses.replace`.

- **`base_url`**: the API root. A relative path in a call is joined onto it (a trailing `/` is added if missing), and
  its origin is the one credentials are sent to.
- **`rate`**: a `Rate`, from which the client builds a budget of its own, or a `RateBudget` it shares with every client
  given the same instance. See [rate budgets](rate-budgets.md).
- **`auth`**: an `httpx2.Auth`, usually one of headgate's. See [auth](auth.md). An upstream `httpx` object raises
  `TypeError`.
- **`observers`**: receive the client's [events](observability.md#events).
- **`middleware`**: per-attempt hooks; see [middleware](#middleware). They are parts of the stack, not settings of how a
  request is sent, so they are the client's own rather than `HttpOptions` fields.

## TransportPolicy

What the stack does to every request.

| Field | Default | |
| --- | --- | --- |
| `retry` | `RetryPolicy()` | how a call is re-sent, or `None` to send every call once; see [retries](retries.md) |
| `timeout` | `Timeouts()` | per-phase timeouts; a phase left unset takes its default, `math.inf` is no limit, and a phase set longer than `deadline` is refused |
| `deadline` | `60.0` | seconds the whole call may take, or `None`; see [deadlines](deadlines.md) |
| `breaker` | `BreakerPolicy()` | the per-origin breaker, or `None`; see [breaker](breaker.md) |
| `cache` | `None` | an `HttpCache`; see [caching](caching.md) |
| `idempotency` | `None` | `IdempotencyKeys()` stamps a key on every POST and PATCH; see [retries](retries.md#idempotency-keys) |
| `routes` | `()` | `Route(fragment, retry=..., deadline=..., rate=..., cache=...)`: the per-path rules, first match wins |

A `Route` changes an endpoint's retry policy, deadline, rate or caching in configuration, so the endpoint method stays
one line:

```python
from headgate import Rate, Route, TransportPolicy

policy = TransportPolicy(
    routes=(
        Route("/charges", retry=None),  # sent once
        Route("/reports", deadline=300.0, rate=Rate.per_minute(6)),  # a long deadline and a lane of its own
        Route("/live", cache=None),  # never cached
    )
)
```

`Route(fragment, /, **options)` takes `RouteOptions`. An option left out takes the policy's (or, for `rate`, the
client's budget), and `None` turns it off: `retry=None` sends once, `deadline=None` sets no deadline, `rate=None`
paces nothing of the route's own, and `cache=None` keeps the paths out of the cache. The first route whose fragment a
call's path holds is the call's route, whole; two routes never combine. A call's own options, and its view's, win
over its route's. An unknown option, or a value of the wrong kind (`retry=False`), raises `TypeError`.

## HttpOptions

How requests are sent and responses read.

| Field | Default | |
| --- | --- | --- |
| `transport` | `None` | a custom network transport (a test double, `headgate.curl.AsyncCurlTransport`); the default is an HTTP/2 `httpx2.AsyncHTTPTransport` |
| `headers` | `{}` | sent on every request to `base_url`'s origin and the `trusted_origins`; any other origin gets only `User-Agent` and the `Accept` headers among them ([security](security.md)); any header the call implies or sets wins a clash (see [precedence](#precedence)) |
| `follow_redirects` | `True` | the client's default; a call can override it |
| `max_redirects` | `5` | the hop limit, auth-flow hops included |
| `max_response_bytes` | 32 MiB | the cap on a buffered body, counted after decompression; a call's own `max_response_bytes=` replaces it for that call; `stream()` is uncapped |
| `response_check` | `None` | a check of every final response; requires `follow_redirects=False` |
| `security` | `SecurityOptions()` | where requests and credentials may go: see below |
| `ssl_context` | `None` | TLS for the default transport: a private CA, mutual TLS, pinning |
| `limits` | 10 connections, 5 kept alive | the default transport's pool |
| `proxy` | `None` | an `http`, `https` or `socks5` proxy URL for the default transport |
| `user_agent` | `None` | your product token, sent before headgate's |
| `cookies` | `False` | `True` keeps httpx2's cookie jar; by default no cookie is stored or sent back |
| `request_id_headers` | `x-request-id`, `request-id`, `x-amzn-requestid`, `x-ms-request-id`, `cf-ray` | where `request_id` is read from |

`SecurityOptions`, passed as `HttpOptions(security=SecurityOptions(...))`, groups the settings that decide where
requests and credentials may go:

| Field | Default | |
| --- | --- | --- |
| `trusted_origins` | `frozenset()` | origins besides `base_url`'s that may receive credentials |
| `block_private_hosts` | `False` | `True` turns on the SSRF guard |
| `resolver` | `None` | how the SSRF guard resolves names; only with the guard on |

`ssl_context`, `limits` and `proxy` configure the default transport, so passing any of them with `transport=` is a
`ValueError`; a custom transport owns its own TLS, pool and proxy. The SSRF guard cannot be combined with `proxy=`.

The default pool (10 connections) sustains about 33 requests a second at 300 ms each; a client budgeted above that
passes wider `limits=httpx2.Limits(...)`.

### The curl transport

`pip install 'headgate[curl]'` adds `headgate.curl.AsyncCurlTransport`, which sends through `curl_cffi` with a
browser's TLS fingerprint, for hosts behind CDNs that refuse other clients:

```python
from headgate import HttpOptions
from headgate.curl import AsyncCurlTransport

http = HttpOptions(transport=AsyncCurlTransport(impersonate="chrome"))
```

It behaves like a native httpx2 transport wherever the stack can tell: it reads no proxy from the environment, keeps no
cookie, follows no redirect (headgate's hooks see every hop), maps curl's error codes onto httpx2's exceptions so the
retry rules apply, and aborts the transfer when a response is closed early, so the size cap holds. It decompresses
bodies itself, so its responses carry no `Content-Encoding`. The SSRF guard vets its hops but cannot pin them.

## Middleware

A middleware sees, and may change, every **attempt** of a request: it runs inside the retry layer, once per send, in
the order given, the first outermost. It calls `call_next(request)` to continue or returns a response of its own:

```python
import uuid

import httpx2

from headgate import Next


class AttemptId:
    # What it stamps as a credential: none, so nothing is withheld from another origin.
    credential_headers = frozenset[str]()
    credential_params = frozenset[str]()

    async def __call__(self, request: httpx2.Request, call_next: Next) -> httpx2.Response:
        request.headers["X-Attempt-Id"] = uuid.uuid4().hex
        return await call_next(request)
```

A signature that must be fresh on every attempt (a timestamp, a nonce) belongs in a
[`SigningAuth`](auth.md#signingauth) passed as the client's `auth=`, not in a middleware: it signs each attempt inside
the retry layer too, after the last middleware and the rate-limit wait, and the class refuses a signer that declares
nothing it stamps.

```python
import hashlib
import hmac
import time
from typing import ClassVar, override

import httpx2

from headgate import SigningAuth


class HmacSigner(SigningAuth):
    # What it stamps: withheld from any hop to an origin other than base_url's and the trusted ones.
    signed_headers: ClassVar = frozenset({"X-Timestamp", "X-Signature"})

    def __init__(self, secret: bytes) -> None:
        self._secret = secret

    @override
    def sign(self, request: httpx2.Request) -> None:
        stamp = str(int(time.time()))
        payload = b"\n".join((request.method.encode(), request.url.raw_path, stamp.encode(), request.content))
        request.headers["X-Timestamp"] = stamp
        request.headers["X-Signature"] = hmac.new(self._secret, payload, hashlib.sha256).hexdigest()
```

A middleware runs inside the transport stack, below the [credential guard](security.md#credentials-stay-on-your-origin)
the client runs as a request hook, so the guard runs again below the last middleware. The headers and query parameters a
middleware declares in `credential_headers` and `credential_params` are removed there from any hop not addressed to
`base_url`'s origin or a trusted one, a cross-origin redirect hop included. Both are required, each a `frozenset` of
names, empty when the middleware stamps none, as a class attribute or a property; the client raises `TypeError` for a
middleware that lacks either, and for a plain function. `request.content` is available for a
buffered body; a streamed body is not readable there. A middleware that replaces the request
(`call_next(httpx2.Request(...))`) keeps `request.extensions` on the new one, or a `SigningAuth` cannot sign that
attempt. Even then the signature goes on the replacement, not on the request the client made, so a `SigningAuth`'s one
re-send on a 401 is lost.

## Per-call options

Every verb, `get`, `post`, `put`, `patch`, `delete` and `head`, has the same signature, except that `head` reads
`expect=nothing` unless it is given another reader, since a HEAD has no body. An option left out takes the view's, the
route's or the client's; `None` turns it off:

```python
await client.get(
    url,  # a literal, a t-string (escaped, and the call's route) or an httpx2.URL (sent as it is)
    expect=reader_or_expect,
    retry=RetryPolicy(attempts=5),  # replaces the view's, the route's or the client's; None: send once
    follow_redirects=False,  # a 3xx raises as itself; True follows it
    cache_ttl=60.0,  # this response's cache lifetime, over HttpCache.ttl; None: no lifetime limit
    params={"q": "gdp"},  # a list or tuple value repeats the key
    headers={"Accept-Language": "en"},
    timeout=Timeouts(read=5.0),  # the phases it sets replace the view's or the client's
    deadline=10.0,  # seconds for this call, or None for none
    max_response_bytes=256 * 1024 * 1024,  # this call's body cap, over HttpOptions.max_response_bytes
)
```

plus `body=` for a body (a `Body`: what a flavor's `writer` wrote, `Json`, `Form`, `Multipart` or `Raw`, see
[request bodies](streaming.md#request-bodies)) and `idempotency_key=` for a write. `stream()` takes the same options
without `expect=`, `cache_ttl=` and `max_response_bytes=`, and `paginate()` takes `expect=`, `pages=`, `limit=` and the
same options but the body, applied to each page; a walk of POSTs takes `query=`
([Walks of POSTs](pagination.md#walks-of-posts)).

`cache_ttl=None` removes headgate's lifetime only: the cache is TTL-only and never reads a response's own cache headers,
so an entry stored with `cache_ttl=None` is served until its storage evicts it, past any `Cache-Control: max-age` and
even past `no-store`, as an entry of `HttpCache(ttl=None)` is.

The options are typed, so a wrapper can forward them with every key checked. `CallOptions` is the TypedDict of every
per-call option but the body, which every verb and `paginate()` take; `RequestOptions` is a verb's (`CallOptions` plus
the body); `headgate.streaming.StreamOptions` is `stream()`'s (`RequestOptions` less `cache_ttl` and
`max_response_bytes`); and `ViewOptions` is what `with_options()` takes. A wrapper typed on them forwards every option
at once:

```python
async def fetch(self, url: LiteralString, **options: Unpack[CallOptions]) -> Series:
    return await self._http.get(url, expect=SERIES, **options)
```

Each is closed, so a misspelled keyword is a type error, and the client refuses one at runtime too, with a `TypeError`
naming it, before anything is sent. `Method` is the literal type of the six verbs, which `stream(method=...)` takes.

### Views

`client.with_options(headers=..., timeout=..., deadline=..., retry=..., follow_redirects=...)` returns a view: the same
client with other defaults for its calls. A view's `retry=None` sends each of its calls once; a call that should not
take the view's defaults is made on the client. It shares everything that makes calls cooperate: the connection pool
and transport stack (so the rate budget and the retry budget), the breakers, the auth and its cached token, and the
observers. Options left out keep the client's; a view of a view layers on the outer view.

```python
patient = client.with_options(timeout=Timeouts(read=120.0), deadline=600.0)
report = await patient.get(t"reports/{report_id}", expect=REPORT)
```

**The client it came from owns the pool**: closing a view, or leaving its `async with` block, closes nothing, and a view
stops working once that client is closed.

### Precedence

For the deadline and the retry policy, the most specific setting wins: the call's own, then the view's, then the first
matching `Route`'s, then the policy's. Headers merge the same way: the call's `headers=` over the view's, then the
headers the call's declarations imply (its body's `Content-Type`, its `Expect`'s `accept=` and `headers=`), then the
client's `HttpOptions.headers`. A transport policy switched off with `policy_disabled()` overrides them all (see
[testing](testing.md#switching-the-policy-off)).
