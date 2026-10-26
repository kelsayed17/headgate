# Changelog

All notable changes to headgate are recorded here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html). Before 1.0, a minor release may
change the public API; such changes are listed under **Changed** and **Removed** with what to write instead.

## [Unreleased]

### Fixed

- Identical concurrent GETs share one cached download again when their timeouts are longer than the time their deadline
  leaves; the sharing key now uses the timeouts the call asked for, not the deadline-cut ones
  ([#4](https://github.com/kelsayed17/headgate/issues/4)).
- A call whose rate-budget turn comes after its deadline fails at once with `ClientTimeoutError` saying it was never
  sent, with `retryable=True` and `retry_after`, instead of waiting and failing like a slow upstream; a retry that
  cannot be sent in time ends the call with the failure it already has, and such a hedge is not sent. A deadline that
  passes before an attempt is sent says so, and `attempts` counts only the sends made
  ([#5](https://github.com/kelsayed17/headgate/issues/5)).

- A cached miss is stored only once the call's reader accepts it, so a refusal inside a 200, an undecodable body or an
  undeclared status is no longer served from the cache for the entry's lifetime
  ([#9](https://github.com/kelsayed17/headgate/issues/9)). A `stream()` is accepted when its head checks pass.

### Changed

- A body past `max_response_bytes` is no longer stored, since it is not held to be written on accept.
- A timeout longer than the deadline it runs under is refused with `ValueError` on the client, a view or a call, since
  it could never take effect ([#6](https://github.com/kelsayed17/headgate/issues/6)). Raise the deadline with it, e.g.
  `with_options(timeout=300.0, deadline=600.0)`.

### Added

- `headgate.ratelimit.BudgetBacklogError`, raised by `RateBudget.wait` when a call's paced turn comes after its deadline.

## [0.2.0] - 2026-10-25

The first release published to PyPI. It reshapes the client around option objects and rebuilds every policy
layer. Code written against the 0.1.0 source needs the migrations below; there are no compatibility aliases.

### Migrating from 0.1.0

| 0.1.0 | 0.2.0 |
| --- | --- |
| `AsyncBaseClient(..., max_retries=N)` | `policy=TransportPolicy(retry=RetryPolicy(attempts=N))`; both count sends, the first included |
| `circuit_breaker=False` | `policy=TransportPolicy(breaker=None)` |
| `rate_limits=(...)` | `policy=TransportPolicy(rate_limits=(...))` |
| `cache=HttpCache(...)` | `policy=TransportPolicy(cache=HttpCache(...))` |
| `default_headers={...}` | `http=HttpOptions(headers={...})` |
| `timeout=30.0` | `http=HttpOptions(timeout=30.0)` (the read timeout) or `timeout=Timeouts(...)` |
| `transport=...` | `http=HttpOptions(transport=...)` |
| `max_response_bytes=...` | `http=HttpOptions(max_response_bytes=...)` |
| `follow_redirects=...` | `http=HttpOptions(follow_redirects=...)` |
| `refusal=check` | `http=HttpOptions(refusal_check=check)` |
| `Refused` | `RefusedError` |
| `Refusal` (the check's type) | `RefusalCheck` |
| `client.stream_get(url)` (an async iterator of chunks) | `async with client.stream("GET", url) as stream: async for chunk in stream.aiter_bytes():` |
| `StaticHeaderAuth.header_names` | `credential_headers` (every auth now has it, and `credential_params`) |
| `CachedTokenAuth.reject_credential_rotation(..., env_var=...)` | `setting=...` |
| `HttpCache(policy, None)` to disable the cache | leave `TransportPolicy.cache` as `None`; `storage` is required |
| the private `_host_breakers` registry | `headgate.breaker.DEFAULT_REGISTRY`, a `BreakerRegistry` |
| `health.breaker_states()` keyed by host | keyed by origin, `scheme://host[:port]`; takes an optional `registry` |

### Added

- **Option objects**: `TransportPolicy` (retry, deadline, breaker, rate rules, cache, idempotency keys, per-route
  overrides) and `HttpOptions` (transport, headers, `Timeouts`, redirects and `max_redirects`, the body cap, the refusal
  check, `trusted_origins`, the SSRF guard and its `resolver`, `ssl_context`, `limits`, `proxy`, `user_agent`,
  `cookies`, `request_id_headers`, `observers`, `middleware`). Frozen and validated on construction; an upstream `httpx`
  object raises `TypeError`.
- **Retries** (`headgate.retry`): `RetryPolicy`, `Backoff`, `RetryBudget`, `Hedge`, `NO_RETRY`. Send tracking decides
  whether a write may be re-sent; 425 and HTTP/2 `REFUSED_STREAM` are retried as unprocessed; `x-should-retry` is
  honoured; a ratio retry budget per client; opt-in hedging; attempt timeouts fitted to the deadline; an attempt history
  in the error's notes. `IdempotencyKeys` and `idempotency_key=` make a POST safe to retry. `Route` sets a retry policy
  or deadline per path.
- **A whole-call deadline**: `TransportPolicy(deadline=60.0)` by default, `deadline=` per call.
- **Rate budgets** (`headgate.ratelimit`): `RateBudget`, shareable between clients, event loops and threads, with GCRA
  pacing, `max_in_flight`, server feedback from `Retry-After`, `RateLimit` / `RateLimit-Policy` and
  `X-RateLimit-Remaining` / `-Reset`, and `Adaptive` throttling, on by default.
- **Breakers** (`headgate.breaker`): `BreakerPolicy` and `BreakerRegistry`, keyed by origin and thresholds, thread-safe
  and bounded. `CircuitOpenError`, a `TransportError`, with `retry_after`.
- **Security** (`headgate.security`): credentials pinned to `base_url`'s origin and `trusted_origins` on every hop,
  https-to-http redirects refused, and an opt-in SSRF guard (`allow_private_hosts=False`) that pins connections to the
  vetted address. `ForbiddenDestinationError`.
- **Cache**: `Partition` (per credential by default, or `SHARED`), `conditional()` reads with `Validator`, `Fresh` and
  `NotModified`.
- **Streaming and bodies**: `stream()` for any method, returning a `Stream` with `aiter_bytes`, `aiter_lines` and
  incremental `aiter_events` (server-sent events with `Last-Event-ID` resume); `content=` (bytes, a one-shot stream, or
  a replayable factory) and `files=` (multipart).
- **Pagination**: `paginate()` with `Cursor`, `Offset`, `LinkHeader` and `NextUrl`, or a `Pager` of your own; lazy, and
  loop-safe.
- **Per-call ergonomics**: `with_options()` views; `retry=` takes a `RetryPolicy`; `Expect(accept=...)` checks the
  `Content-Type`; `Expect(headers=...)`.
- **Errors**: every `ClientError` carries `method`, `url`, `request_id`, `attempts`, `retryable` and `retry_after`;
  `UpstreamStatusError.problem` parses RFC 9457 problem details (`Problem`); `ResponseParseError` has `status_code` and
  `content_type`. Every error pickles.
- **Observability**: typed events in `headgate.events` delivered to `Observer`s; `headgate.otel.OpenTelemetryObserver`
  (the `otel` extra) with one CLIENT span and duration per call and `traceparent` on every attempt; `Deprecated`
  events for `Deprecation` and `Sunset` headers; a `User-Agent` naming headgate; `health.observer_failures()`.
- **Middleware**: per-attempt `Middleware` inside the retry layer, for request signing.
- **Testing kit** (`headgate.testing`): `FakeTransport`, `FaultyTransport` and its fault steps, `TestClock`,
  `instant_retries()`, `isolated_breakers()` and `CassetteFilter` for vcrpy.
- `Method` and `RequestOptions` are public; `RequestOptions` gains `content`, `files`, `deadline` and
  `idempotency_key`, and its `timeout` takes `Timeouts`.
- `CachedTokenAuth.seed()`, `token_headers` and `credential_identity()`; `Minted` for mint flows.
- The `cache` extra (`pip install 'headgate[cache]'`), which brings `hishel[async]` and so the `anysqlite` that
  hishel's `AsyncSqliteStorage` needs.
- Documentation: a guide per area in `docs/`, a design document, and tested examples.

- `headgate.MAX_MINT_RESPONSE_BYTES`, the cap on the response to each hop of a token mint.

### Changed

- **Only safe re-sends.** 0.1.0 retried every method, POST included, after any failure. A POST or PATCH is now re-sent
  only when nothing of it was written, the server proved it unprocessed, or it carries an idempotency key.
- **Retried statuses** are 408, 425, 429, 500, 502, 503 and 504; 501 and other 5xx are no longer retried, and local
  errors (an unencodable header, an unsupported scheme, an invalid URL) are never retried.
- **`Retry-After`** is waited exactly up to `RetryPolicy.max_retry_after` (30 s) instead of being clamped to 5 s; a longer
  one raises at once with `retry_after` set.
- **Retry budget.** Retries are capped per client at 20% of first attempts plus 10 a second.
- **A 60-second deadline** now bounds every call by default, retries and waits included; pass `deadline=None` (per call,
  per route or in the policy) for none.
- **The breaker** is keyed by origin (scheme, host and port) instead of host, keeps separate breakers for clients with
  different thresholds, no longer counts a 429 or a failed token mint, and stops a retry sequence as soon as it opens.
- **Rate limiting** reacts to the server: a 429/503 or exhausted rate-limit headers pause the shared budget, and
  adaptive throttling lowers the rate on pushback. Pass `RateBudget(rate, adaptive=None)` for fixed pacing.
- **Cookies** are neither stored nor sent back unless `HttpOptions(cookies=True)`.
- **`HttpCache`** is keyword-only, its `storage` is required, and a client whose auth does not declare its credentials
  cannot use a credential-partitioned cache (`ClientConfigurationError`).
- **Codecs** take a type expression (`TypeForm`), so `decoder(list[A] | A)` is typed exactly.
- Error messages name the request line and a sanitized, bounded URL, and fold an RFC 9457 problem's title and detail in
  place of the raw body.
- The example client is `examples/series_client.py`; the test suite uses `httpx2-pytest`.

### Removed

- The `aiolimiter` dependency: pacing is headgate's own GCRA budget.
- `AsyncBaseClient`'s individual keyword options, `stream_get()`, `Refused`, `Refusal` and `header_names` (see the
  migration table).
- The repository's `headgate_pytest_alias.py` plugin and the golden retry table, replaced by behaviour and property
  tests of the new retry policy.

### Fixed

- The client and its transport no longer read proxy variables or `.netrc` from the environment (`trust_env=False`), as
  the no-environment contract always said.
- curl transport: closing a response early no longer downloads the rest of the body; cached gzip bodies replay;
  mid-body curl errors map onto httpx2 exceptions; `Timeout(None)` no longer crashes; curl error codes map to the right
  httpx2 exceptions.
- Cache: an over-cap body no longer leaks its pooled connection; the network URL is no longer reordered; cancellation
  no longer leaks responses; concurrent GETs share a download only when every header matches, so a `Range`, `Accept`
  or credential difference no longer returns another request's response; a shared failure counts once toward the
  breaker, not once per waiting request.
- Auth: a token mint's hops are marked, so their failures are no longer charged to the API host's breaker; a mint flow
  that ends without a token raises a clear error; mint failures map onto the `ClientError` tree, naming the token
  endpoint.
- `ClientError.retryable` is the retry policy's verdict on the outcome: a surviving 501 or 505, a 503 marked
  `x-should-retry: false`, a 503 on a POST without an idempotency key, and a body-read failure on such a POST are no
  longer reported retryable.
- Auth: a mint flow is handed each hop's response read (bounded to 1 MiB), so `.json()` and `.content` work instead of
  raising `httpx2.ResponseNotRead`; an oversized or dropped token response, an `httpx2.StreamError` and an
  `httpx2.CookieConflict` map onto the `ClientError` tree. Every hop gets the call's timeouts, cut to its deadline,
  unless it sets its own.
- A hedged request no longer sends one more attempt than `RetryPolicy.attempts` when `Hedge.max_extra` allows more
  hedges than the attempts left.
- A redirect chain past `max_redirects` is not retryable and not charged to the origin's breaker: the same request
  would loop the same way, and the origin answered every hop.
- A surviving 5xx the retry policy does not re-send (a 501, a 505) is worded `upstream 501 ...`; `transient` is kept for
  the statuses the policy retries.
- A non-ASCII `Content-Length` no longer crashes; error snippets no longer decode a whole 32 MiB body.
- Backoff no longer overflows past 1023 retries or produces a NaN wait; `Rate` refuses NaN and infinite values.
- `sanitize_url` no longer rewrites untouched parts of a URL and never raises; `event_stream` skips data-less blocks;
  `drop_none` walks tuples.
- A hand-written reader's `ValueError`, `LookupError` or `TypeError` on a 2xx is a `ResponseParseError`; a
  `refusal_check` that raises `RefusedError` is treated like one that returns it.

### Security

- A middleware that declares the credentials it stamps (`credential_headers`, `credential_params`) has them withheld
  from any untrusted origin: the credential guard runs again below the last middleware, so a signer no longer signs a
  cross-origin redirect hop.
- Credentials stay on trusted origins on every hop: an absolute URL to another origin, a redirect, or a cursor URL no
  longer carries `Authorization`, an auth's declared headers or a `QueryParamAuth` key. 0.1.0 withheld only a static
  header, and only on a cross-origin redirect.
- The cache is partitioned by credential and stores no credential, cookie or `Set-Cookie`; 0.1.0 could serve a
  response cached under one credential to a request made with another, and stored the request's credential headers.
- A redirect from `https` to `http` is refused.
- A header value the HTTP library refuses to encode, usually the credential, no longer appears in the error or its
  traceback; parse errors no longer quote body values; URL userinfo is redacted whole, and the list of secret query
  parameters covers OAuth, session and signed-URL parameters; a cache backend's message, which can carry its DSN, is
  never repeated.
- A URL an error names has its control characters percent-encoded, so a URL taken from a response (a cursor, a
  redirect) cannot forge a line in a log that records the error.

## [0.1.0] - 2026-10-24

The initial version, in the repository only (it was not published to PyPI): an async HTTP client base on httpx2 for
Python 3.14.

### Added

- `AsyncBaseClient` with one method per verb, each taking the call's declared responses as `expect=`: a reader or an
  `Expect` with exact non-2xx answers, anything else raising a `ClientError` subtype.
- Retries with exponential backoff and `Retry-After`, per call opt-out with `retry=False`.
- A token-bucket rate limit per client (`Rate`), with per-path `RateRule`s.
- A per-host circuit breaker spanning the send and the body read.
- A response body cap counted on decompressed bytes.
- `StaticHeaderAuth`, `QueryParamAuth` and `CachedTokenAuth`.
- An opt-in `HttpCache` (hishel) with `PathFragmentFilter`, `MethodFilter` and `StatusCodeFilter`, degrading to the
  network when its storage fails.
- t-string URL paths that escape every interpolated value as one segment.
- The `headgate.msgspec` and `headgate.pydantic` codec flavors, the optional `AsyncCurlTransport` (`headgate[curl]`),
  `headgate.health` snapshots and `headgate.policy.policy_disabled()`.

[Unreleased]: https://github.com/kelsayed17/headgate/compare/159a3c2...HEAD
[0.2.0]: https://github.com/kelsayed17/headgate/compare/f319ad8...159a3c2
[0.1.0]: https://github.com/kelsayed17/headgate/commit/f319ad8
