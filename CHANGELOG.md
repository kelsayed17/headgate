# Changelog

All notable changes to headgate are recorded here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html). Before 1.0, a minor release may
change the public API; such changes are listed under **Changed** and **Removed** with what to write instead.

## [Unreleased]

A walk of POSTs is written as a `post` is: `paginate()` takes its body as `body=` or `json=`, and the writer that
encodes a `body=` belongs to the pager, built once with the endpoint's reader. A review of the whole library fixes a
cache that could serve one client's private response to another, credentials sent where they should not go, a
rate-limit pause that let queued calls stampede, and errors from a `stream()` block mistaken for the transport's. This
is a minor release: see **Changed** and **Removed**.

### Added

- `paginate()` takes a walk's body as `body=` (your own value) or `json=` (builtins, sent as JSON), as `post()` does;
  either as `None` is no body, so an optional body is one call. Overloads make a GET walk under a body pager, a body
  walk under a GET-only pager, a `body=` under a pager with no `write`, a body of another type than the pager's, and
  `body=` beside `json=` type errors. See [Walks of POSTs](docs/pagination.md#walks-of-posts).
- `write=` on `Cursor`, `Offset`, `BodyCursor` and `BodyOffset`: the writer (a codec's `encoder`) that encodes a
  walk's `body=` for every page, whatever request the pager returns. The new `WritingPager` base class is a `Pager`
  with that `write` property: a custom pager walked with `body=` subclasses it, so a pager writes bodies because it
  declares so, never because of an attribute's name.

### Changed

- `RequestOptions` and `PageOptions` are closed TypedDicts (PEP 728), so a keyword a verb or `paginate()` does not
  name, such as a misspelled option, is a type error. So is a TypedDict that subclasses one of them to add keys: declare
  your own keys in a TypedDict of your own and pass them beside the options, not inside them.
- `PageOptions` no longer has the body keys (`body`, `json`, `data`, `content`, `files`), which `paginate()` refused
  at runtime; `paginate()` takes `body=` and `json=` itself. A dict typed `RequestOptions` that is splatted into
  `paginate()` should be typed `PageOptions`.
- A walk's body is sent as its keyword says, and a mismatch raises `ValueError` instead of being sent another way:
  `body=` and `json=` together; `data=`, `content=` or `files=` ("paginate takes no data=, content= or files=: a walk
  of POSTs sends body= or json="); a `body=` whose pager has no `write`; and a `json=` whose pager has one. Every page
  with a body is held to these rules and to the refusal of a fixed idempotency key, not only the first.
- `paginate()` calls the pager's `first()` once, when the walk is built, and every iteration starts from the request it
  returned, so a custom `first()` must be a function of the request alone (nothing that should change between
  iterations, such as the time). A pager's refusals (`LinkHeader` or `NextUrl` given a body, `BodyCursor` or
  `BodyOffset` given none) now raise from the `paginate()` call rather than from the first iteration.

- `HttpCache(methods=)` accepts only `GET` and `HEAD`, and raises `ValueError` for any other method, since entries are
  keyed by URL: cache a read-only POST with `policy=FilterPolicy(use_body_key=True, ...)`. An empty `methods=` or
  `statuses=` now stores nothing, as `paths=()` already did, instead of falling back to the defaults.
- Every request header the HTTP cache keeps is stored as a keyed digest, never as sent. The key is the process's, so an
  entry whose response varies on a header is not found again after a restart, and expires by its TTL.
- A `CachedTokenAuth` mint hop redirected to another origin raises `ForbiddenDestinationError` instead of being
  followed, so the hop's own secret headers and form body never leave the token origin.
- A server can pause a `RateBudget` for at most an hour, so a `Retry-After` dated in the year 9999 no longer stops every
  client that shares the budget.
- A rate-limit pause holds every waiting call: calls already queued, or waiting for a `max_in_flight` slot, resume in
  order at the configured rate when it ends, not all at once. A call now takes its concurrency slot before its paced
  turn.
- Breaking: `RetryPolicy.wait_after` takes the `Retry-After` delay already parsed (`float | None`) instead of the
  failed response, so the delay waited is the one checked against `max_retry_after` and recorded.
- A per-call `deadline=` and `Route(deadline=)` refuse infinity and NaN, as `TransportPolicy` and `with_options` do,
  all with one message.
- A 302 answering a PUT, PATCH or DELETE with a one-shot `content=` stream is followed as a body-less GET, as httpx2
  follows it, instead of raising the 302; a 301 on a method other than POST, which resends the body, is refused.
- `content=` given a `str`, `bytearray`, `memoryview` or a plain iterable raises a `TypeError` naming `content=`,
  instead of failing inside httpx2.
- `stream().aiter_events()` and `event_stream` share a parser of headgate's own that follows the WHATWG event-stream
  grammar (see **Fixed**); `event_stream` now yields an event whose `data:` line is empty, as the grammar does.
- `TestClock` overlaps concurrent sleeps, as real time does: two calls backing off 30 s together both retry at 30, not
  30 and 60. A sleep cancelled before it wakes no longer moves the clock.
- `check_codec` checks that a codec's messages leave out an input's keys as well as its values, and cuts the malformed
  body at its middle, so a codec whose output ends in a newline (NDJSON, orjson's `OPT_APPEND_NEWLINE`) passes.
- Docs: `min_attempt_time` holds back retries, hedges and waits; a request's first attempt, a redirect or mint hop
  included, is always sent.

### Removed

- `paginate()` no longer takes a `PageRequest` as its URL. Write `paginate(url, params=params, body=body, ...)` for
  `paginate(PageRequest(url, params, body=body, write=write), ...)` and put `write=` on the pager, or pass `json=body`
  for a request that had no `write`. `PageRequest` stays public as what a `Pager` reads and returns.
- `PageRequest.write`: how a body is sent is the walk's, set by the pager's `write`, not each request's.

### Fixed

- The HTTP cache no longer stores, or shares across credentials, a key that a middleware stamps (declared in
  `credential_headers`) or that is set in `HttpOptions(headers=...)`. Those values partition entries under
  `Partition.CREDENTIAL`, so clients with different keys sharing one storage no longer read each other's responses.
- Under `Partition.SHARED`, query parameters with a secret name such as `key` or `session` that the auth does not
  declare no longer collapse into one entry: each value gets its own.
- With `HttpOptions(cookies=True)`, a `Cookie` header set explicitly (client-wide or per call) is withheld from
  untrusted origins; those origins receive only the cookies the jar holds for them.
- The SSRF guard refuses IPv6 site-local addresses (`fec0::/10`), which Python's `ipaddress` reports as global.
- `CachedTokenAuth` no longer discards its token and mints again on a 401 from a request sent without the token
  (withheld from an untrusted origin).
- A `DecodeError` message no longer quotes the input: msgspec drops an unknown tag and an unknown field's name, and
  pydantic shows `[...]` for an unknown tag and for a key of the body's own, such as a `dict[str, int]` key.
- `stream()` no longer turns an exception its block raises itself into a `TransportError`: an `OSError` or httpx2 error
  from the caller's own code propagates unchanged and is not charged to the origin's breaker, and `CallFinished` (and
  so the OpenTelemetry span) reports the stream's status with no error.
- `head()`, and a 204 or 304, no longer fail with `ResponseTooLargeError` when `Content-Length` is past
  `max_response_bytes`, and a reader of a response with no body sees its `Content-Length` and `Content-Encoding`.
- `max_in_flight` no longer leaks a slot for a response that arrives already read, such as one from
  `httpx2.MockTransport` or `FakeTransport`, where the next call used to hang.
- With several windows, `X-RateLimit-Remaining` and `X-RateLimit-Reset` pause for the reset of the window that is
  spent, paired by position, not the latest reset of any window.
- A call refused by a paused rate budget reports `attempts=0`, with no history line and no `AttemptFinished`; on a
  retry it ends with its real last failure.
- A retry or hedge that is never sent (the breaker opened, the deadline came, the rate budget refused it, or the call
  was cancelled) gives its share of the `RetryBudget` back.
- The backoff before retry *n* counts retries only, so a hedge no longer doubles the next wait.
- An `EncodedBody`'s `Content-Type` and `Expect.headers` win over a header in `HttpOptions.headers`, losing only to the
  call's or its view's `headers=`.
- A built-in `TimeoutError` from a middleware or custom transport raises `ClientTimeoutError`, not `TransportError`.
- `stream(last_event_id=...)` replaces a `Last-Event-ID` set in `headers=` in any letter case, instead of sending both.
- `stream().aiter_events()` and `event_stream`: a leading byte order mark no longer loses the first event, the body is
  read as UTF-8 whatever its `charset`, a block with no data (an `event: ping` heartbeat, or `id:` alone) is no longer
  yielded with empty `data`, and `retry:` takes only ASCII digits.
- `Stream.last_event_id` is `None` until the server sends an `id`, and `""` only once the server resets it; an `id:`
  in a block without data sets it too.
- `Deprecated.link` and `sunset_link` read `Link` relations as RFC 8288 defines them, space-separated and
  case-insensitive, so `rel="deprecation sunset"` and `rel=Deprecation` are found.
- `DropMidBody` and `RefusedStream` report send progress as httpx2's transport does, so a POST with a body that is
  dropped before anything is written is retried like one without a body.
- A `WritingPager` subclass that leaves out `first`, `page_items` or `next_page` cannot be built, instead of inheriting
  a stub that returns `None` mid-walk.

## [0.8.0] - 2026-10-27

Pages endpoints whose query is a POST body: `paginate()` walks POSTs, and `BodyCursor` and `BodyOffset` carry the page
token or offset in the body. A walk of GETs is written as before; the refusals and messages that changed are listed
under **Changed**, so this is a minor release.

### Added

- Walks of POSTs: `paginate()` takes a `PageRequest(url, params, body=..., write=...)` as its first argument, and each
  page is then a `post` of the body, written by `write` (a codec's `encoder`) or sent as `json=`. Two new pagers carry
  the page token in that body: `BodyCursor(items=, next=, put=)` puts each cursor into the next page's body, and
  `BodyOffset(items=, page_size=, offset=, put=)` advances an offset the body carries. A POST page keeps the rule for
  a method that is not idempotent: it is re-sent only under `IdempotencyKeys` (a fresh key per page) or a `retry=`
  that treats POST as idempotent. See [Walks of POSTs](docs/pagination.md#walks-of-posts).
- `PageRequest.body`, `PageRequest.write` and `PageRequest.with_body()`, and a third type parameter on `Pager`,
  `Cursor` and `Offset`: the type `B` of the walk's body, which is `None`, as before, for a walk of GETs.

### Changed

- `paginate()` refuses `body=` (as it refused the other body options), `params=` beside a `PageRequest`, and on a walk
  of POSTs a fixed idempotency key (`idempotency_key=`, or the key's header in `headers=`); `LinkHeader` and `NextUrl`
  refuse a walk with a body. A `PageLimitError` or pagination-loop `ClientError` from a walk of POSTs names `POST` as
  its method. The message for a body option on `paginate()` is now "paginate takes no body option: a walk of POSTs
  passes its body in a PageRequest".
- Messages, docs and docstrings use American spelling. The one message whose text changes is the refusal of a token
  endpoint that rotates its long-lived credential, which now says "Re-authorize".

## [0.7.0] - 2026-10-26

Makes headgate codec-agnostic: the core speaks to a serialization library only through a `Codec`, msgspec and pydantic
are two codecs among any, and a plain `pip install headgate` brings neither. An install that relies on msgspec needs
the `headgate[msgspec]` extra, so this is a minor release: see **Changed**.

### Added

- `Codec`, the protocol a serialization library implements to plug in: its `media_type`, the `errors` it raises for a
  body of the wrong shape, a `decoder` and an `encoder` per type, and a `describe` that never quotes the input.
  `decoder_for(codec, Type)`, `encoder_for(codec, Type)` and `decode_with(codec, content, Type)` build a reader, a
  writer and a decode on any codec; a codec's own errors are raised as `DecodeError`, so a call raises them as
  `ResponseParseError` instead of outside the `ClientError` tree. See the new
  [codecs guide](https://kelsayed17.github.io/headgate/codecs/).
- `headgate.msgspec.CODEC` and `headgate.pydantic.CODEC` (`MsgspecCodec`, `PydanticCodec`): the two built-in codecs,
  under the shortcuts each module already had.
- `headgate.testing.check_codec(codec, record=...)`, which holds a codec to the promises the built-in ones keep:
  strict types, malformed bytes refused, unknown fields ignored, and messages that never repeat the input.

### Changed

- msgspec is an extra: install `headgate[msgspec]` where you installed `headgate`, as `headgate[pydantic]` already
  was. `import headgate.msgspec` without it fails with an `ImportError` naming the extra.

## [0.6.0] - 2026-10-26

Makes the public API smaller and more regular: the auth hooks lose their underscores, `head()` and `paginate()` take
what the other verbs take, path rules match whole segments, timeouts are always a `Timeouts`, request bodies get a
writer to mirror `expect=`, and each package exports only what a client is written with. Much of it changes existing
code, so this is a minor release: each change below says what to write instead.

### Added

- A request-body writer, the mirror of `expect=`: each codec flavor's `encoder(Type, *, drop_none=False)` builds a
  `Writer[Type]`, and a call sends what it wrote as `body=`: `NEW_ORDER = encoder(NewOrder)`, then
  `await self.post("orders", body=NEW_ORDER(order), expect=ORDER)`. The writer takes only its own type, msgspec
  encodes a struct straight to bytes, and the body is sent as `application/json` unless `headers=` sets another
  `Content-Type`. `EncodedBody`, `Writer` and `json_writer` (for a codec of your own) are exported from `headgate`.
- `rate=` is optional: without it a client paces nothing itself (`headgate.ratelimit.UNLIMITED`) but still waits out
  the pauses the server asks for, a 429 or 503 with `Retry-After` and a spent `RateLimit` quota.
- `Rate.per_hour(requests)`, for a provider that states its quota hourly.
- `BearerAuth(token)`, for the most common `StaticHeaderAuth({"Authorization": f"Bearer {token}"})`.
- `HttpCache(methods=, paths=, statuses=)` say what is stored without hishel's `FilterPolicy`: the GETs of `paths`
  (every path by default) answered with a status in `statuses` (200 by default). `policy=` stays, now optional, for
  a `FilterPolicy` of your own.
- `Next`, the type of a middleware's `call_next`, and `ViewOptions`, the options `with_options()` takes, are exported
  from `headgate`.
- `FakeTransport.reply()` takes a `Respond`, the step `FaultyTransport` answers with, beside a response, a status or a
  function: `Respond(503, retry_after=2.0)`.

### Changed

- `HttpOptions`' `trusted_origins`, `allow_private_hosts` and `resolver` move into a group of their own,
  `SecurityOptions`, exported from `headgate`: write `HttpOptions(security=SecurityOptions(allow_private_hosts=False))`
  where you wrote `HttpOptions(allow_private_hosts=False)`.
- `CachedTokenAuth`'s subclass hooks are public: override `mint_token`, `mint_flow`, `attach` and `skips` where you
  overrode `_mint_token`, `_mint_flow`, `_attach` and `_skips`. A hook under its old name is no longer called, so a
  subclass that defines only `_mint_token` or `_mint_flow` fails when it is defined.
- `head()` reads `expect=nothing` when no `expect=` is given: `await client.head(url)` returns `None`.
- `RateRule`, `Route` and `PathFragmentFilter` match whole path segments instead of any substring: `"/exports"`
  matches `/v2/exports/orders.csv` but no longer `/v2/exports-archive`. A fragment that names no segment (`""`, `"/"`)
  is refused with `ValueError`, by `RateRule` and `PathFragmentFilter` too, which used to accept it and match every
  request.
- `timeout=` takes only a `Timeouts`, on `HttpOptions`, `with_options()` and every call; a bare number is no longer
  accepted. A `Timeouts` now sets only the phases it names: a phase left unset takes the view's, the client's or the
  default (connect 10 s, read 30 s, write 10 s, pool 15 s), so write `Timeouts(read=60.0)` where you wrote `60.0`. A
  call's or a view's `Timeouts` used to replace all four phases.
- A decoder's `check=` is `refusal_check=`, the name `HttpOptions` already uses for the same job on the response head:
  `decoder(Envelope, refusal_check=refusal_in)`. `json_reader` takes it under the same name.
- `CachedTokenAuth(refresh_margin_s=)` is `CachedTokenAuth(refresh_margin=)`, in seconds like every other duration.
- In `headgate.testing`, `Respond(status=)` is `Respond(status_code=)`, and `FakeTransport.route()` is
  `FakeTransport.reply()`, so it no longer shares a name with `headgate.Route`.
- `stream()` takes the URL first and the method as `method=`, a GET by default: `client.stream("export.csv")`, and
  `client.stream("jobs", method="POST")` where you wrote `client.stream("POST", "jobs")`.
- `with_options()` takes its options as `ViewOptions`, and an option left out keeps the outer setting: passing
  `headers=None`, `timeout=None` or `retry=None` to mean "keep" is no longer accepted; leave the option out.
- The `event_stream` reader returns each event whole (`httpx2.ServerSentEvent`, with `event`, `data`, `id` and
  `retry`), as `Stream.aiter_events()` yields them; read `.data` where you used the bytes it returned.
- `HttpCache`'s filters take any iterable (`MethodFilter({"GET"})`, `StatusCodeFilter([200])`), and `MethodFilter`
  matches a method in any case.
- `CachedTokenAuth.reject_credential_rotation` is an instance method named for the auth class, as every mint failure
  is: call `self.reject_credential_rotation(returned, configured, setting=...)` without `provider=`.
- `RateBudget.pause_remaining` is a property.
- `Paginated` and `Stream` are protocols: what `paginate()` and `stream()` return, typed, but not built directly.
- `Offset(limit=)` is `Offset(page_size=)`, so it no longer shares a name with `paginate(limit=)`, the page budget.
- `paginate()` takes `follow_redirects=` and `cache_ttl=`, applied to every page as on `get()`. Its options are typed
  by the new `PageOptions`, `RequestOptions` plus those two.

### Fixed

- `paginate()` sends an `Expect`'s `headers` and `accept` on every page and checks each page's `Content-Type` against
  `accept`, as a verb does; it used to drop both.

### Removed

- From the root `headgate` package, names that live in a subpackage and are rarely needed to write a client. Import
  them from there:
  - `headgate.retry`: `Backoff`, `Hedge`, `RetryBudget`, `NO_RETRY`;
  - `headgate.ratelimit`: `Adaptive`;
  - `headgate.breaker`: `BreakerRegistry`;
  - `headgate.cache`: `Fresh`, `MethodFilter`, `NotModified`, `Partition`, `PathFragmentFilter`, `StatusCodeFilter`,
    `Validator`, `conditional`;
  - `headgate.pagination`: `Pager`, `PageRequest`;
  - `headgate.events`: `Observer`;
  - `headgate.streaming`: `Stream`.
- From the subpackages, the machinery the client assembles itself from, which no client calls:
  - `headgate.retry`: `RetryTransport`, `SendTrackingTransport`, `REPLAYABLE_BODY`;
  - `headgate.ratelimit`: `RateLimitTransport`, `PathBudgets`, and the `BudgetBacklogError` and `BudgetPausedError`
    signals, which a call raises as `ClientTimeoutError` and `RateLimitError`;
  - `headgate.breaker`: `BreakerOpenError` (raised as `CircuitOpenError`), `Transition`, `Verdict`,
    `DEFAULT_REGISTRY`, `DEFAULT_MAX_HOSTS`;
  - `headgate.security`: `CredentialGuard`, `CHAIN_ORIGIN`, `address_hook`, `refuse_https_downgrade`.
- `BreakerState` from `headgate.events` and `headgate.health`: import it from `headgate.breaker`, its one home.
- `headgate.policy.policy_is_disabled`; `policy_disabled` stays.
- `RateBudget.wait()`, `observe()`, `occupy()` and `now()`, which only the client's rate-limit layer called, and
  `Expect.accepts()`, which only the client called.

## [0.5.0] - 2026-10-26

Adds `Paginated.collect()`, so a walk becomes a list in one call, bounded by items as well as pages. Nothing changes for
existing code.

### Added

- `Paginated.collect(max_items=None)` returns a walk's items as a list; with `max_items`, at most that many, fetching
  no page past the one that fills it ([#19](https://github.com/kelsayed17/headgate/issues/19)).
- A [standards page](https://kelsayed17.github.io/headgate/standards/) mapping each RFC and draft headgate follows to
  what it does, and a comparison with about 30 HTTP, retry, rate-limit, cache, breaker and testing libraries.

## [0.4.0] - 2026-10-26

Bounds `paginate()`: a page budget, and a stop at pages with no items. The stop changes what three pagers do by
default, so this is a minor release: see **Changed**.

### Added

- A page budget for `paginate()`: `limit=PageLimit(max_pages)` raises the new `PageLimitError` once the budget is spent
  with pages still to come, or with `on_limit="stop"` ends the walk there
  ([#14](https://github.com/kelsayed17/headgate/issues/14)).
- A documentation site at <https://kelsayed17.github.io/headgate/>, built from `docs/`, with a page comparing headgate
  with tenacity, stamina and a hand-rolled client.
- `examples/github_client.py`: a tested client for a few GitHub REST endpoints.

### Changed

- `Cursor`, `NextUrl` and `LinkHeader` stop at a page with no items, as `Offset` already did, instead of following its
  link: a link that advances while returning nothing made the walk spend requests for nothing. Pass
  `stop_on_empty=False` for an endpoint that sends empty pages mid-walk
  ([#15](https://github.com/kelsayed17/headgate/issues/15)).

## [0.3.0] - 2026-10-26

Fixes four bugs where a call's deadline, its timeouts, the rate budget and the cache disagreed. One of them makes a
configuration that could never work an error, so this is a minor release: see **Changed**.

### Added

- `headgate.ratelimit.BudgetBacklogError`, raised by `RateBudget.wait` when a call's paced turn comes after its deadline.

### Changed

- A timeout longer than the deadline it runs under is refused with `ValueError` on the client, a view or a call, since
  it could never take effect ([#6](https://github.com/kelsayed17/headgate/issues/6)). Raise the deadline with it, e.g.
  `with_options(timeout=300.0, deadline=600.0)`.
- A body past `max_response_bytes` is no longer stored in the cache, since it is not held to be written on accept.

### Fixed

- A cached miss is stored only once the call's reader accepts it, so a refusal inside a 200, an undecodable body or an
  undeclared status is no longer served from the cache for the entry's lifetime
  ([#9](https://github.com/kelsayed17/headgate/issues/9)). A `stream()` is accepted when its head checks pass.
- Identical concurrent GETs share one cached download again when their timeouts are longer than the time their deadline
  leaves; the sharing key now uses the timeouts the call asked for, not the deadline-cut ones
  ([#4](https://github.com/kelsayed17/headgate/issues/4)).
- A call whose rate-budget turn comes after its deadline fails at once with `ClientTimeoutError` saying it was never
  sent, with `retryable=True` and `retry_after`, instead of waiting and failing like a slow upstream; a retry that
  cannot be sent in time ends the call with the failure it already has, and such a hedge is not sent. A deadline that
  passes before an attempt is sent says so, and `attempts` counts only the sends made
  ([#5](https://github.com/kelsayed17/headgate/issues/5)).

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
  honored; a ratio retry budget per client; opt-in hedging; attempt timeouts fitted to the deadline; an attempt history
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
- The repository's `headgate_pytest_alias.py` plugin and the golden retry table, replaced by behavior and property
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

[Unreleased]: https://github.com/kelsayed17/headgate/compare/v0.8.0...HEAD
[0.8.0]: https://github.com/kelsayed17/headgate/compare/v0.7.0...v0.8.0
[0.7.0]: https://github.com/kelsayed17/headgate/compare/v0.6.0...v0.7.0
[0.6.0]: https://github.com/kelsayed17/headgate/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/kelsayed17/headgate/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/kelsayed17/headgate/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/kelsayed17/headgate/compare/159a3c2...v0.3.0
[0.2.0]: https://github.com/kelsayed17/headgate/compare/f319ad8...159a3c2
[0.1.0]: https://github.com/kelsayed17/headgate/commit/f319ad8
