# headgate public API redesign

Status: for approval · 2026-11-05

## Why

headgate's behavior is strong: one stack where a cache hit is never retried, a retry waits its rate turn and one
deadline bounds the lot; credentials that never follow a redirect or reach the cache; standards followed by default.
Its public API is not yet as good as its behavior. Four adversarial reviews of the API (a first-hour user, an
API-design purist, an extender, and a type-system and misuse tester), each checked against the code, found the same
kinds of fault again and again:

- **Safety that depends on spelling.** The secure path and the insecure one look almost the same, and nothing
  catches the slip.
- **Concepts that overlap.** Several option bags, each drawing its line somewhere else; one word meaning three
  things; four spellings of "off".
- **Seams that need private imports**, and shared state shared three different ways.
- **Types that do not tell the truth**, so a wrong call type-checks and returns garbage.

headgate is pre-1.0, and the changelog already says a minor release may change the public API. Now is the cheapest
time to fix the shape once, before users depend on it.

**Success** is a public API where the obvious way to write a call is the safe one, each concept has one name and one
home, every seam an extension needs is public and typed, and a type checker rejects the common mistakes the reviews
found. A newcomer should be able to write the README's client without learning more than the client, a reader, and
the option groups.

## Goals

1. Make the safe spelling the only one that type-checks (URLs, signing, credential declarations).
2. Give each concept one name and one home: one set of per-call options, one place for per-path rules, one meaning
   for "off", one naming rule for config values versus shared state.
3. Make readers a real type, so a wrong `expect=` is a type error.
4. Publish every type an extension needs; nothing public may require importing from `headgate._core` or another
   underscore module.
5. Make shared state uniform: something shared between clients is an object you construct and pass.
6. Shrink the top level to what a client is written with.

## Non-goals

- A synchronous client.
- Replacing or wrapping httpx2's `Request`/`Response` types; they stay the currency of readers, pagers and
  middleware.
- Changing what the stack does (retry rules, pacing, the breaker's charging, the cache's credential rules). This is a
  reshaping of the API over the same behavior, except where a section below says otherwise.
- RFC 9111 cache mode, WebSockets, and new transports. They can follow on the new shape.

## Anti-goals

These would make the redesign a failure even if every item below shipped:

- **Losing any secure default** the current API has, or adding a way to send a credential somewhere it is not sent
  today.
- **Adding a concept without removing the one it replaces.** Every change here removes the old spelling; none leaves
  two ways to say one thing.
- **Two ways to do one thing**, anywhere: two client styles, two body spellings, two meanings of `None`.
- **Deprecation shims that outlive the release.** See "Migration".

## Constraints

- Python 3.14 or newer; httpx2 2.16 or newer; hishel 2 (the port in PR #55 lands first).
- The project's gates stay as they are: ruff with every rule, ty with every rule as an error, no suppression
  comments, 100% line and branch coverage, the API-surface snapshot (`tests/package/test_api_surface.py`), the test
  that every exported name appears in the docs, typos (American English), and a `CHANGELOG.md` entry.
- The property tests in `tests/retry/test_properties.py` must keep the same properties and assertions: they pin
  behavior this redesign must not alter. Their constructor spellings and imports are updated mechanically as names
  move (`clock=` to `timing=`, `_core` imports to the public ones); nothing else in the file changes.
- Bug fixes already in PR #54 (`json=` with `data=`, a call's `retry=True` over its view's, kind checks at
  construction, `paginate` not going through `self.get`) and PR #55 (hishel 2) are the starting point. Sections
  3.3, 4.2 and 5.4 later remove what two of those fixes touch (`json=`/`data=`, `retry=True`); the fixes still land,
  since `main` ships between phases.

## The design

Each item says what changes and what is removed. Where a choice was mine rather than settled with the maintainer, it
says so ("my call"), so it can be checked while reading. The maintainer's rule for every choice, mine included: the
cleanest design, even where it breaks existing code.

### 1. Safety by construction

**1.1 URLs.** Every verb, `stream` and `paginate` takes `url: LiteralString | Template | httpx2.URL`.

- A t-string is escaped per segment, as now.
- A literal string is trusted structure, as a `str` is now.
- `httpx2.URL` is the explicit "verbatim" spelling, for a URL that came from the wire. Pagers already forward
  next-page URLs and will pass them as `httpx2.URL`.
- A computed `str` such as `f"items/{uid}"` stops type-checking. Today it silently skips escaping: with
  `uid="../../admin"` the call reaches `/admin`. ty already rejects `str` where `LiteralString | Template` is expected.
- Runtime still accepts any `str`, since `LiteralString` cannot be checked at runtime. This is a type-level guard.

**1.2 One signing seam.** `SigningAuth` signs on every attempt, inside the retry layer.

- Today it signs once per call above the retry layer, so a retry re-sends a stale timestamp. That pushes HMAC signers
  toward middleware, which has none of `SigningAuth`'s enforcement.
- The auth flow marks the request; a transport inside the retry layer calls `sign()` for each attempt. It keeps
  `SigningAuth`'s existing guarantees: the class declares what it stamps (`signed_headers`), is refused at class
  definition when it declares nothing, and re-signs after a 401.
- `credential_identity()` keeps feeding the cache partition.
- Middleware stops being the recommended home for signatures; `docs/configuration.md`'s HMAC example moves to
  `SigningAuth`.

**1.3 Credential declarations are required.** The `Middleware` protocol requires `credential_headers` and
`credential_params` (each a `frozenset[str]`, empty when it stamps nothing).

- `HttpOptions` construction raises `TypeError` for a middleware without both, or with another type. Today a typo
  (`credential_header`) or a missing half silently sends the secret to another origin.
- This breaks every existing middleware until it adds two lines. The break is the point (my call).

### 2. Readers

**2.1 `Reader[T]` is a real class.** `headgate.Reader` becomes a final generic class wrapping a function of the
response.

- Every reader headgate ships is an instance: `nothing`, `as_bytes`, `event_stream`, codec readers.
- A hand-written reader is `Reader(fn)`. `Reader` has a `repr` naming what it reads, so `Expect(...)` prints
  something useful.
- `expect=` takes `Reader[T] | Expect[T]` only. `expect=str` and `expect=Model` stop type-checking; today they return
  `'<Response [200 OK]>'` or a model holding a response object.
- `Expect` can then be told from a reader by type, which removes the constraint that kept `Expect` from being
  callable (`_responses.py`). Whether `Expect` becomes callable is left to the builder.

**2.2 One vocabulary for building readers and writers.**

- `reader_for(codec, T, *, allow_empty_body=False, refuse=None) -> Reader[T]` and `writer_for(codec, T) -> Writer[T]`
  replace `decoder_for` and `encoder_for`.
- Flavor modules mirror them: `headgate.msgspec.reader(T)` / `.writer(T)`, and the same for pydantic.
- `body_reader(decode, *, empty=None, refuse=None)` stays as the escape hatch, with the refusal keyword spelled the
  same as the others. Its `empty=` takes the bytes to decode in place of an empty body, since without a codec it
  cannot encode an empty array itself; the codec builders' `allow_empty_body=` is that same choice made for you.
  (Amended with the phase 2 plan.)
- `as_json` is a ready reader using the standard library's `json.loads`, so a core-only install can read JSON without
  learning codecs. It is named beside `as_bytes`, and not `JSON`, which would differ from the `Json` body class (3.3)
  only in case. (Amended with the phase 2 plan.)
- Removed: `json_reader` (which, despite its name, hands raw bytes to its function), `json_writer`, `decoder_for`,
  `encoder_for`, flavor `decoder`/`encoder`.
- `Codec.decoder` and `Codec.encoder` stay: they are byte functions, the codec's own surface.

**2.3 `refusal_check` is split by what it sees.**

- `HttpOptions(response_check=...)` sees the response (today's `refusal_check` and `RefusalCheck`).
- `refuse=` on a reader sees the decoded value (today's reader `refusal_check=`).
- `RefusedError` moves to `headgate.errors`, beside `DecodeError`.

### 3. One set of per-call options

**3.1 `CallOptions`.** One `TypedDict` holds every per-call option except the URL, `expect=` and the body:
`params`, `headers`, `timeout`, `deadline`, `retry`, `follow_redirects`, `cache_ttl`, `idempotency_key`.

- Every verb, `stream`, `paginate` and `with_options` takes `**options: Unpack[...]` of it or a documented subset.
- `retry`, `follow_redirects` and `cache_ttl` stop being separate parameters, so a wrapper typed on the options can
  forward them. Today it cannot. Their values follow 4.2: `retry=` is a `RetryPolicy` or `None` (send once), and
  leaving it out inherits; `True` and `False` are gone.
- `RequestOptions` = `CallOptions` plus `body=` (3.3).
- `ViewOptions` = the subset a view can default: `headers`, `timeout`, `deadline`, `retry`, `follow_redirects`.
- `stream` takes `CallOptions` minus `cache_ttl`, plus `method` and `last_event_id`.
- `expect=` and `pages=` stay named parameters, since they drive the return type.

**3.2 A walk's body is `query=`.** `paginate(query=value)` names the raw value the pager rewrites per page, and
replaces both of today's walk keywords: `body=` (the raw value, encoded by the pager's `write=`) and `json=` (builtins,
sent as JSON). The pager's `write=` encodes the value when it has one; without one the value must be builtins and is
sent as JSON, as `json=` is today. `body=` then keeps one meaning everywhere: the request body (3.3). Today
`body=` means the encoded value on a verb and the raw value on `paginate`, so copying a call between them breaks.
My call.

**3.3 One `body=`.** The five mutually exclusive body keywords (`body=`, `json=`, `data=`, `content=`, `files=`)
become one parameter, `body: Body | None`, whose value says what it is:

| Value | Sent as | Replaces |
| --- | --- | --- |
| `EncodedBody` (from a `writer_for` writer) | its bytes, with its `Content-Type` | `body=` |
| `Json(value)` | JSON of builtins | `json=` |
| `Form(fields)` | `application/x-www-form-urlencoded` | `data=` |
| `Multipart(fields, files)` | `multipart/form-data` | `files=` with `data=` |
| `Raw(content)` | bytes, an async iterable of bytes (streamed once, so not re-sent), or a callable returning one (re-sent) | `content=` |

`Json`, `Form`, `Multipart` and `Raw` are small frozen classes in `headgate`. A conflicting body can no longer be
written, so `check_one_body` and its five refusals are deleted. My call.

### 4. Configuration grouped by what it is for

**4.1 What goes where.**

| Group | Holds |
| --- | --- |
| `HttpOptions` | How bytes reach the server: transport, TLS context, pool limits, proxy, default headers, user agent, cookies, redirect policy (`follow_redirects`, `max_redirects`), `max_response_bytes`, `request_id_headers`, `security` |
| `TransportPolicy` | What the stack does with a call: `retry`, `timeout` (moved here from `HttpOptions`, beside the `deadline` it is checked against), `deadline`, `breaker`, `cache`, `routes` |
| Client constructor | `observers=` and `middleware=`, which are parts of the stack, not settings of one layer |

- `idempotency` stays on `TransportPolicy`. Keys are stamped on every POST and PATCH whatever the retry setting
  (a server deduplicates by them too), and `paginate`'s one-key refusal reads them; moving them into `RetryPolicy`
  would make them vanish under `retry=False`. The retries guide points to them instead. My call.
- `HttpOptions(response_check=)` (2.3) keeps its rule that it needs `follow_redirects=False`.
- SSRF guard: `SecurityOptions(allow_private_hosts=False)` becomes `SecurityOptions(block_private_hosts=True)`,
  removing the double negative two levels deep. The default stays "allowed". My call.

**4.2 `None` is off; leaving an option out inherits. Everywhere.**

- `TransportPolicy(retry=None)` means no retries; `NO_RETRY` is removed.
- `rate: Rate | RateBudget | None = None`, where `None` means no pacing of its own (server pauses still apply);
  `UNLIMITED` (a real `Rate` of a million per second) is removed.
- `breaker`, `cache` and `idempotency` already use `None`.
- An override (a call, a view, a `Route`) inherits whatever it leaves out, and its `None` is off, exactly as in a
  configuration value. `deadline=` already works this way: `deadline=None` is no deadline, and leaving it out
  inherits.
  - A call's `retry=None` sends once; leaving `retry` out inherits the view's, the route's or the policy's.
  - A `Route(retry=None)` sends once on that path, and `Route(rate=None)` paces nothing of its own there.
  - A call's `cache_ttl=None` stores without a lifetime limit; leaving it out takes the cache's `ttl`.
- Overrides are keyword bags, so "left out" is real absence, not a sentinel: `CallOptions` and `ViewOptions` are
  `TypedDict`s (3.1), and `Route(fragment, **options: Unpack[RouteOptions])` takes a `TypedDict` too.
- `retry=True`, which undid a view's setting, is removed along with `retry=False`: a call that should not take the
  view's defaults is made on the client, not the view. This replaces PR #54's call-level semantics. My call.
- `follow_redirects` stays a `bool`: it is a switch, not a policy.

**4.3 One per-path rule.** `Route(fragment, **options)`, with `retry`, `deadline`, `rate` and `cache`, is the only
per-path rule.

- The first route whose fragment matches the path is the call's route, whole: what it sets applies, and what it
  leaves out comes from the policy. Two routes never combine. Today `routes` and `rate_limits` are matched
  independently, so a call can take its rate from one rule and its retry from another; a configuration that relied
  on that is rewritten as one route per path. My call.
- `RateRule` and `TransportPolicy.rate_limits` are removed; `Route(..., rate=)` takes a `Rate` or a shared
  `RateBudget`.
- `cache=False` on a route keeps that path out of the cache. `HttpCache(paths=)` stays as the cache's own filter, for
  a cache that should keep only a few paths.
- A route may also match a call's t-string template (`Route(t"orders/{id}")`), not only a path fragment. Whether to
  include this is left to the builder.

### 5. Seams and shared state

**5.1 Public seam types.**

- `headgate.security` exports `CredentialStamper` and `CredentialCarrier`. The cache's own error message today tells
  users to implement `headgate._core.CredentialStamper`.
- A new `headgate.timing` module exports `Timing`, `Clock` and `RandomSource`.
- Every injectable clock takes `timing: Timing`. Today it is `clock=`, `timing=` or `wall_clock=` depending on the
  class.
- A new `headgate.call_info(request) -> CallInfo | None` gives middleware a read-only view of the call: route
  template, attempt number, time left before the deadline. Today it takes `headgate._core.CallContext`.

**5.2 Naming.**

- A `*Policy` or `*Options` dataclass is a frozen config value.
- A `*Budget` or `*Registry` is live shared state: constructed once and passed to every client that shares it, with
  a `repr` showing its state.
- Per-call keyword bags are `*Options` `TypedDict`s (3.1).
- Under this rule `RetryBudget` becomes live shared state like `RateBudget`, shared by passing the same object; today
  it is a frozen value with hidden per-client state. `RetryPolicy.budget` defaults to `None`, and a client with no
  budget given makes its own, shared by every route and view of that client: today's per-client behavior, unchanged.
  Only a `RetryBudget` object the caller constructs and passes to several clients is shared between them. A
  `RetryPolicy` stays frozen and hashable, holding the budget by identity. My call.
- `BreakerRegistry` is already an object you construct.

**5.3 Breakers are shared only when a registry is passed.** Each client keeps its own breakers by default, one per
origin. Clients that should pool their failure counts are given the same `BreakerRegistry`:
`BreakerPolicy(registry=shared)`.

- This is 5.2's rule applied to breakers: shared state is an object you construct and pass, as with `RateBudget` and
  `RetryBudget`.
- It removes the process-wide registry, headgate's last hidden global state. `headgate.testing.isolated_breakers`,
  which exists only to keep tests apart from that global registry, is removed.
- `headgate.health.breaker_states(registry)` reports the registry it is given.
- Today two clients on one origin share a breaker only when their thresholds happen to be equal, because the global
  registry keys by origin and thresholds, and `breaker_states` reports the most degraded one. A passed registry holds
  one breaker per origin; a client whose thresholds differ from the ones that registry's breaker for that origin was
  made with raises `ValueError` at construction.
- This is a behavior change, the one exception to the non-goal: two clients on one origin no longer pool failures
  unless given one registry. The maintainer asked for the cleanest design; this is my recommendation.

**5.4 A client is held, not inherited.** `AsyncBaseClient` becomes `headgate.Client`, a final concrete class. An
API client holds one:

```python
class GitHub:
    def __init__(self, token: str) -> None:
        self._http = Client(base_url=API, auth=BearerAuth(token), rate=RateBudget(Rate.per_second(10)))

    async def repository(self, owner: str, name: str) -> Repository | None:
        return await self._http.get(t"repos/{owner}/{name}", expect=REPOSITORY_OR_NONE)
```

- One way to write a client, and the SDK's namespace is its own: an SDK's `get(order_id)` cannot shadow the
  transport's `get`, its consumers do not inherit `post(anything)`, and its `self._timeout` cannot break the client.
  Each of those hazards is removed outright, not guarded against. Today each is reachable by subclassing.
- `Client` supports `async with` and `aclose()`, and an SDK forwards them in a few lines; `docs/` shows how.
- `with_options` returns a `Client` view, as it returns a view of the subclass today.
- The README's example, `examples/` and every guide are rewritten to hold a `Client`. `StubClient` in the tests
  becomes a plain `Client`.
- Testing an SDK no longer needs its author to forward `transport=`: `headgate.testing` gains a way to build every
  `Client` in a block over a fake transport (the builder picks its shape, alongside the existing construction-time
  switches it replaces).
- My call.

### 6. Errors

- **`retryable` means one thing:** sending this call again later could succeed. `CircuitOpenError` and a budget
  pause become `retryable=True` with their `retry_after`. Today `CircuitOpenError` says `retryable=False` with
  `retry_after≈60`, so the recipe in `docs/errors.md` re-raises the one error that says "come back in a minute".
- **`sent`** is a new read-only attribute: whether any byte of the request reached the network.
- A refusal by headgate's own rate budget carries `status_code=None` rather than another call's 429.
- Every error attribute is a read-only property. Today `UpstreamStatusError.status_code` can be assigned.
- Using a closed client keeps raising `RuntimeError`, which is pinned by a test and deliberate: it is a programming
  error, like the `ValueError` for a bad t-string.

### 7. Streams, pages and smaller items

- **Streams.**
  - A stream's read timeout is not checked against the deadline, which bounds only opening it.
  - `Timeouts` gains a way to say "no limit" for a phase (`UNBOUNDED`); `None` keeps meaning "inherit".
  - `client.events(url, ...)` opens a server-sent-event stream with `Accept: text/event-stream` and no read limit.
- **Pages.**
  - `Paginated[I, P = object]`, item type first, so a return annotation is `Paginated[Issue]`.
  - `paginate(max_pages=, on_limit=)` replaces `limit=PageLimit(...)`, which collided with every API's `limit` query
    parameter.
  - `Offset(size_param=...)` sends the page size itself.
  - Pager constructors that bind the page type from the reader (`Cursor.of(reader, ...)`), so a module-level pager
    written with lambdas keeps its item type, are left to the builder.
- **QUERY.** httpx2 2.6 added the safe, idempotent QUERY method. headgate gains a `query()` verb, and QUERY joins the
  idempotent methods in `retry/_policy.py` and the known methods in `otel.py`.
- **Kind checks.** Every option is checked for its kind at construction or at the call, with a `TypeError` naming the
  option (PR #54 started this). `paginate`'s limit is the known gap.
- **Nested query values.** A nested `params` value (a mapping, a set, bytes) raises `TypeError` instead of being sent
  as its Python `repr`.

### 8. The top level

`headgate.__all__` keeps about 48 names:

| Area | Names |
| --- | --- |
| The client | `Client`, `RequestOptions`, `CallOptions`, `ViewOptions` |
| Configuration | `TransportPolicy`, `HttpOptions`, `SecurityOptions`, `Timeouts`, `Route`, `RouteOptions`, `IdempotencyKeys`, `Middleware` |
| Policy values | `RetryPolicy`, `BreakerPolicy`, `Rate`, `RateBudget`, `HttpCache` |
| Responses | `Expect`, `Reader`, `Writer`, `Codec`, `reader_for`, `writer_for`, `body_reader`, `as_json`, `as_bytes`, `nothing`, `event_stream` |
| Bodies | `Body`, `EncodedBody`, `Json`, `Form`, `Multipart`, `Raw` |
| Auth | `BearerAuth`, `StaticHeaderAuth`, `QueryParamAuth`, `SigningAuth`, `CachedTokenAuth`, `Minted` |
| Pages | `Paginated` |
| Errors | `ClientError`, `TransportError`, `ClientTimeoutError`, `UpstreamStatusError`, `AuthError`, `RateLimitError` |

Everything else lives once, in its subpackage:

- the pagers, in `headgate.pagination`;
- the other errors and `Problem`, in `headgate.errors`;
- the seam types (5.1), in `headgate.security` and `headgate.timing`.

`Method` and `Next` stay at the top level, beside the `stream(method=)` and `Middleware` that need them (two more
names than the table). `DEFAULT_TIMEOUTS` becomes the class attribute `Timeouts.DEFAULT`, and
`MAX_MINT_RESPONSE_BYTES`, which no option sets, becomes private, with its value stated in the auth guide. My call.

No name is exported from two places.

## Migration

- One breaking minor release (0.12), with every removal and rename listed in `CHANGELOG.md` under **Changed** and
  **Removed** with what to write instead.
- No deprecation aliases: a clean break, as the maintainer chose.
- Each phase below lands as its own pull request with its tests, docs and changelog. Each pull request leaves `main`
  green and internally consistent, even though the release is cut only after the last one.

## Phases

1. Safety by construction (section 1).
2. Readers (section 2).
3. Per-call options and the walk's `query=` (section 3).
4. Configuration groups, "off" and routes (section 4).
5. Seams, naming, shared state, and `Client` by composition (section 5).
6. Errors (section 6).
7. Streams, pages and smaller items (section 7).
8. The top level (section 8), last, once every name has its final home.

Phases 2 and 3 touch the same signatures and are best done in that order. Phases 1, 6 and 7 are independent of the
rest.

## Left to the builder

- Internal structure: how the per-attempt signing transport is built, and how `Reader` stores its function.
- Whether `Expect` becomes callable once `Reader` is a class.
- Whether `Route` also matches t-string templates (4.3).
- Pager constructors that bind the page type (7).
- Exact `repr` formats, and the wording of every error message, which follows the project's style of naming the
  option, what it takes, and what it got.
- Test layout, as long as each changed behavior has a test that fails without the change.
