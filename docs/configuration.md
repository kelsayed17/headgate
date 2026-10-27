# Configuration

```text
AsyncBaseClient(
    *,
    base_url: str,
    rate: Rate | RateBudget = UNLIMITED,
    auth: httpx2.Auth | None = None,
    policy: TransportPolicy = TransportPolicy(),
    http: HttpOptions = HttpOptions(),
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

## TransportPolicy

What the stack does to every request.

| Field | Default | |
| --- | --- | --- |
| `retry` | `RetryPolicy()` | how a call is re-sent; see [retries](retries.md) |
| `deadline` | `60.0` | seconds the whole call may take, or `None`; see [deadlines](deadlines.md) |
| `breaker` | `BreakerPolicy()` | the per-origin breaker, or `None`; see [breaker](breaker.md) |
| `rate_limits` | `()` | `RateRule`s: per-path budgets; see [rate budgets](rate-budgets.md#per-path-lanes) |
| `cache` | `None` | an `HttpCache`; see [caching](caching.md) |
| `idempotency` | `None` | `IdempotencyKeys()` stamps a key on every POST and PATCH; see [retries](retries.md#idempotency-keys) |
| `routes` | `()` | `Route(fragment, retry=..., deadline=...)`: per-path overrides, first match wins |

A `Route` changes an endpoint's retry policy or deadline in configuration, so the endpoint method stays one line:

```python
from headgate import Route, TransportPolicy
from headgate.retry import NO_RETRY

policy = TransportPolicy(routes=(Route("/charges", retry=NO_RETRY), Route("/reports", deadline=300.0)))
```

## HttpOptions

How requests are sent and responses read.

| Field | Default | |
| --- | --- | --- |
| `transport` | `None` | a custom network transport (a test double, `headgate.curl.AsyncCurlTransport`); the default is an HTTP/2 `httpx2.AsyncHTTPTransport` |
| `headers` | `{}` | sent on every request; a call's `headers=` wins a clash |
| `timeout` | `Timeouts()` | per-phase timeouts; a phase left unset takes its default |
| `follow_redirects` | `True` | the client's default; a call can override it |
| `max_redirects` | `5` | the hop limit, auth-flow hops included |
| `max_response_bytes` | 32 MiB | the cap on a buffered body, counted after decompression; `stream()` is uncapped |
| `refusal_check` | `None` | a host-wide check of every final response; requires `follow_redirects=False` |
| `security` | `SecurityOptions()` | where requests and credentials may go: see below |
| `ssl_context` | `None` | TLS for the default transport: a private CA, mutual TLS, pinning |
| `limits` | 10 connections, 5 kept alive | the default transport's pool |
| `proxy` | `None` | an `http`, `https` or `socks5` proxy URL for the default transport |
| `user_agent` | `None` | your product token, sent before headgate's |
| `cookies` | `False` | `True` keeps httpx2's cookie jar; by default no cookie is stored or sent back |
| `request_id_headers` | `x-request-id`, `request-id`, `x-amzn-requestid`, `x-ms-request-id`, `cf-ray` | where `request_id` is read from |
| `observers` | `()` | receive the client's [events](observability.md#events) |
| `middleware` | `()` | per-attempt hooks; see [middleware](#middleware) |

`SecurityOptions`, passed as `HttpOptions(security=SecurityOptions(...))`, groups the settings that decide where
requests and credentials may go:

| Field | Default | |
| --- | --- | --- |
| `trusted_origins` | `frozenset()` | origins besides `base_url`'s that may receive credentials |
| `allow_private_hosts` | `True` | `False` turns on the SSRF guard |
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
the order given, the first outermost. It calls `call_next(request)` to continue or returns a response of its own. An
`httpx2.Auth` flow runs once per call above the retry layer, so a signature that must be fresh on every re-send (a
timestamp, a nonce) belongs in a middleware:

```python
import hashlib
import hmac
import time

import httpx2

from headgate import Next


class HmacSigner:
    # What it stamps: withheld from any hop to an origin other than base_url's and the trusted ones.
    credential_headers = frozenset({"X-Signature"})
    credential_params = frozenset[str]()

    def __init__(self, secret: bytes) -> None:
        self._secret = secret

    async def __call__(self, request: httpx2.Request, call_next: Next) -> httpx2.Response:
        stamp = str(int(time.time()))
        payload = b"\n".join((request.method.encode(), request.url.raw_path, stamp.encode(), request.content))
        request.headers["X-Timestamp"] = stamp
        request.headers["X-Signature"] = hmac.new(self._secret, payload, hashlib.sha256).hexdigest()
        return await call_next(request)
```

A middleware runs inside the transport stack, below the [credential guard](security.md#credentials-stay-on-your-origin)
the client runs as a request hook, so the guard runs again below the last middleware. The headers and query parameters a
middleware declares in `credential_headers` and `credential_params` are removed there from any hop not addressed to
`base_url`'s origin or a trusted one, a cross-origin redirect hop included. A middleware that declares nothing has its
headers sent wherever the request goes, so it must check the destination itself. `request.content` is available for a
buffered body; a streamed body is not readable there.

## Per-call options

Every verb, `get`, `post`, `put`, `patch`, `delete` and `head`, has the same signature, except that `head` reads
`expect=nothing` unless it is given another reader, since a HEAD has no body:

```python
await client.get(
    url,  # a t-string (escaped, and the call's route) or a str (sent as it is)
    expect=reader_or_expect,
    retry=True,  # False: send once; a RetryPolicy: replace the client's for this call
    follow_redirects=None,  # None: the client's default
    cache_ttl=None,  # this response's cache lifetime, over HttpCache.ttl
    params={"q": "gdp"},  # a list or tuple value repeats the key
    headers={"Accept-Language": "en"},
    timeout=Timeouts(read=5.0),  # the phases it sets replace the view's or the client's
    deadline=10.0,  # seconds for this call, or None for none
)
```

plus `body=` (what a codec's `encoder` wrote), `json=`, `data=`, `content=` or `files=` for a body and `idempotency_key=` for a write. `stream()` takes the same
options without `expect=` and `cache_ttl=`, and `paginate()` takes `expect=`, `pages=`, `limit=` and every other option
but a body, applied to each page.

### Views

`client.with_options(headers=..., timeout=..., deadline=..., retry=...)` returns a view: the same client with other
defaults for its calls. It shares everything that makes calls cooperate: the connection pool and transport stack (so
the rate budget and the retry budget), the breakers, the auth and its cached token, and the observers. Options left
out keep the client's; a view of a view layers on the outer view.

```python
patient = client.with_options(timeout=Timeouts(read=120.0), deadline=600.0)
report = await patient.get(t"reports/{report_id}", expect=REPORT)
```

**The client it came from owns the pool**: closing a view, or leaving its `async with` block, closes nothing, and a view
stops working once that client is closed.

### Precedence

For the deadline and the retry policy, the most specific setting wins: the call's own, then the view's, then the first
matching `Route`'s, then the policy's. Headers merge the same way: the call's over the view's over the client's
`HttpOptions.headers`. A transport policy switched off with `policy_disabled()` overrides them all (see
[testing](testing.md#switching-the-policy-off)).
