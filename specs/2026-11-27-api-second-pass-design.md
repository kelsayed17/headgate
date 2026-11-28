# headgate public API: the second pass

Status: approved · 2026-11-27. The maintainer asked for every finding of the review to be fixed ("do everything").

## Why

The 0.12 redesign (`specs/2026-11-05-public-api-redesign-design.md`) shipped all eight phases. Four adversarial
reviews of 0.12.1 then repeated the method that produced it, each with a different lens: a first-hour user writing an
SDK from the docs, an API-design purist reading the surface snapshot, an extender plugging in auth, middleware,
observers, pagers, codecs and transports, and a type-system and misuse tester running ty and the runtime on every
mistake. Each finding was reproduced with a script; the security findings were reproduced twice.

The core holds up: most newcomer mistakes are type errors and runtime errors that name the fix. What remains falls into
five kinds, the same kinds the first redesign named, now at the edges it did not reach:

- **Safety that depends on spelling, at the seams.** Middleware must declare its credentials; an `auth=` need not, and
  its key follows a redirect to another origin. A declaration of `frozenset("X-Api-Key")` declares eight letters. A
  GET with a body is served another body's cached response. A custom pager can take a walk from https to http.
- **One idea with several names.** Three spellings of "the credentials I stamp", two ways to mint a token, "query"
  meaning three things, breaker thresholds kept in two places.
- **Mechanism in public.** Methods only the stack calls, some with private types in their signatures, sit on public
  classes, while three types public signatures use cannot be imported.
- **Late or misleading failures.** `Json(struct)` fails inside httpx2; `Reader(str)` returns `'<Response [200 OK]>'`;
  an `async def sign` sends the request unsigned.
- **Drift.** Docs that show calls the code refuses.

**Success** is the first redesign's goal, met at every seam: the obvious spelling is the safe one, each concept has one
name and one home, every type an extension needs is public and nothing else is, and a mistake fails where it is made,
with a message naming the fix.

## Goals

1. Every credential headgate stamps or carries is declared, and the declaration cannot be misspelled into a leak.
2. A secure default that holds for headgate's own pieces holds for a user's piece in the same seam.
3. One name per concept, one home per name, everywhere, including the module a class reports in tracebacks.
4. The public surface is what users write and read; what only the stack drives is private.
5. Every option is checked for its kind where it is given, with a `TypeError` naming it and saying what to write.
6. The docs show only code that runs, and a test runs what they show.

## Non-goals

- New features beyond what a finding needs. Where a fix needs a new name (`as_text`), it says what it replaces.
- A synchronous client, new transports, RFC 9111 cache mode.
- Changing retry, pacing, breaker charging or cache credential rules, except where a section below says so.

## Anti-goals

The first redesign's, unchanged: never lose a secure default; never add a concept without removing the one it replaces;
never two ways to do one thing; no deprecation shims. Plus one: **never fix a finding by documenting it**. Where the
docs say "is not covered", the fix covers it.

## Constraints

The first redesign's, unchanged: Python 3.14+, httpx2 2.16+, hishel 2; ruff with every rule, ty with every rule an
error, no suppression comments, 100% line and branch coverage, the API-surface snapshot, the test that every exported
name appears in the docs, typos, a `CHANGELOG.md` entry per change with what to write instead. The properties and
assertions of `tests/retry/test_properties.py` do not change; only their spellings follow renames.

## The design

Findings are cited by the reviewer's id (F first-hour, P purist, E extender, T types). Where reviewers proposed
different fixes, the one chosen is stated and the other is named, with why.

### 1. Safety at the seams

**1.1 `auth=` takes a declared auth (E1, T1, P11).** `Client(auth: Auth | None)`, where `headgate.Auth` is the one
type an auth is: an `httpx2.Auth` that declares `credential_headers` and `credential_params` and implements
`credential_identity()`. `BearerAuth`, `StaticHeaderAuth`, `QueryParamAuth`, `SigningAuth` and `CachedTokenAuth` are
all `Auth`s.

- ty rejects a plain `httpx2.Auth`; `Client()` raises `TypeError` for one, naming the class and saying to subclass
  `headgate.Auth` (or use `StaticHeaderAuth`) and declare what it stamps. `httpx2.BasicAuth` and `DigestAuth` are
  refused too: `StaticHeaderAuth({"Authorization": ...})` or a three-line `Auth` covers them, and an exception would
  be a second rule.
- Removed: the guard's "not covered" carve-out (`security/_credentials.py`, `docs/security.md`), and the cache's
  separate `ClientConfigurationError` for an undeclared auth, which becomes unreachable.
- Left to the builder: whether `Auth` is an abstract base class all five derive from (my lean: yes, since `httpx2`'s
  own `Auth` is a class and the declarations are class attributes) or a protocol.

**1.2 A declaration cannot split into letters (T2).** Wherever credential names are read (middleware at `Client()`,
`Auth` subclasses at class definition, `CassetteFilter`), a non-empty set whose names are all one character long is
refused with a `TypeError` that shows the `frozenset({...})` spelling. No real header or query credential is one
character long.

**1.3 A GET or HEAD has no body (T3).** `get` and `head` take `**options: Unpack[CallOptions]`, which has no `body`;
runtime refuses `body=` on both and points to `query()`, the safe read with a body. `delete` keeps `RequestOptions`.
As defense in depth, the cache never stores, serves or shares in flight a request whose body is not empty.

**1.4 The walk owns its URL rules (E2).** Resolving a pager's next `httpx2.URL` against the URL that answered, and
refusing an https-to-http step, move from the built-in strategies' private `_absolute` into the walk itself, so they hold
for every `Pager`. The strategies' copy is removed.

**1.5 An `httpx2.URL` is absolute (T4, T11).** A call's URL is a literal `str`, a t-string or an absolute
`httpx2.URL`. A relative `httpx2.URL` is refused (`TypeError`, pointing to the t-string and `{fragment:raw}`), since
its only use is skipping escaping; every URL a pager forwards is absolute. Any other kind (`None`, `bytes`, a list) is
refused at runtime too.

**1.6 Only values with a wire meaning are sent (F1, T8).** One allowlist encodes every value of `params=`, `Form` and
`Multipart` fields: `str`, `int`, `float`, `bool` and `None` as today; an `Enum` as its `.value` (which must itself be
a `str` or `int`); `UUID` and `Decimal` as `str()`; `date`, `datetime` and `time` as `isoformat()`. Anything else
raises `TypeError` naming the key and its type. An allowlist, because a pydantic model's `str()` is `x=1`.

**1.7 Collections of names are collections (T9).** Parameters that take names or codes are typed `Collection[...]`
and checked with `check_names`, which refuses a bare `str`: `CassetteFilter(headers=, params=)`,
`HttpCache(methods=, paths=, statuses=)`, `RetryPolicy(methods=, statuses=)`. `CassetteFilter(auths=)` takes
`Iterable[CredentialCarrier]` and refuses anything else.

**1.8 A token that is not text is attached by its owner (E8).** A `CachedTokenAuth` whose token type is not `str` must
override `attach`: refused at class definition when the type argument can be read from the class's original bases,
and at the first `attach` otherwise. The default never sends a token's `repr`.

### 2. Auth, one way

**2.1 One declaration (P4, E6).** Every seam declares what it stamps in `credential_headers` and `credential_params`
class attributes. Removed: `SigningAuth.signed_headers`/`signed_params`, `CachedTokenAuth.token_headers`/
`token_params`, the properties that mapped them, and `CachedTokenAuth`'s either-or check. `CachedTokenAuth`'s default
is `credential_headers = frozenset({"authorization"})`. A `SigningAuth`, and a `CachedTokenAuth` that overrides
`attach`, must declare at least one name, as today.

**2.2 One mint (F2, E7, P15).** `CachedTokenAuth.mint(request)` is the one abstract hook: an async generator that
yields each request the exchange needs and ends with a `Minted`. A mint with no HTTP hop yields only `Minted`.
Removed: `mint_token` (which opened its own `httpx2.AsyncClient`, outside the stack and `fake_network`), `mint_flow`
(renamed), the tuple shape, and the hand-rolled "override one of two" check. `seed(minted: Minted[TokenT])` takes the
same shape.

**2.3 An auth needs no base constructor (E14).** `CachedTokenAuth`'s state is created on first use, and
`refresh_margin` is a class attribute (`refresh_margin: ClassVar[float] = 120.0`, checked at class definition). A
subclass that forgets `super().__init__()` therefore cannot break, and the base has no `__init__` to forget.

**2.4 The flow is not a hook (E13).** `async_auth_flow` is `@final` on `SigningAuth` and `CachedTokenAuth`, and so is
`reject_credential_rotation`. Their hooks are `sign`, `skips`, `attach` and `mint`; the docs point a per-request header
at middleware.

**2.5 Signing reads the client's clock (E12, P8).** `SigningAuth.sign(request, now)` receives the wall-clock time of
the attempt from the client's `Timing`, so a signer runs on `TestClock.install()` like everything else, and the docs'
`time.time()` calls go.

**2.6 Hooks have the right color (T13, E14).** A coroutine-function `sign` is refused at class definition; an observer
whose `notify` is a coroutine function, and a middleware whose `__call__` is not one, are refused at `Client()`.

### 3. One name per concept

**3.1 The walk's value is `payload` (P1, T16).** `paginate(payload=)`, `PageRequest.payload`,
`PageRequest.with_payload()`. "Query" is left to the QUERY verb and, in prose, the URL query. (T16's `seed` was
rejected: `CachedTokenAuth.seed` already exists.)

**3.2 A pager carries its reader (T5, F4, P14).** Every pager takes its page reader as its first positional argument
(`Reader[P] | Expect[P]`), so lambdas are checked against the page type:
`LinkHeader(ISSUE_PAGE, items=lambda p: p.items)`. `paginate(url, *, pager=..., ...)` drops `expect=`, and the keyword
is `pager=`, not `pages=` (which `Paginated.pages()` uses for pages). Generic parameters read item first everywhere:
`Pager[I, P, B]`, `Cursor[I, P, B]`, matching `Paginated[I, P]`. The `Pager` protocol gains the `read` attribute. No
`Cursor.of(...)` beside the constructor: one spelling.

**3.3 Breakers take the rate's shape (P2).** `CallPolicy(breaker: BreakerPolicy | BreakerRegistry | None)` and
`BreakerRegistry(policy: BreakerPolicy = BreakerPolicy(), /, *, max_hosts=1024)`, as `rate: Rate | RateBudget` and
`RetryBudget(share)`. Removed: `BreakerPolicy.registry`, `BreakerRegistry.bind`, its mismatch `ValueError`, and the
threshold keywords on lookup.

**3.4 `CallPolicy`, holding the rate (P5, P18).** `TransportPolicy` becomes `CallPolicy`, the pair of `CallOptions`,
leaving "transport" to the network layer and its failure. `rate=` moves from the `Client` constructor into it, beside
the routes that override it. `RouteOptions` gains `timeout` (P24), since every other override layer has both.

**3.5 Smaller one-name fixes.**

- `Expect(ok, *, headers=, accept=)`: `answers` is filled only by `.on()` (F7, P17).
- `call_info` becomes `call_view(request) -> CallView | None`; `CallView.call` becomes `CallView.info`; `CallInfo.route`
  becomes `CallInfo.template`, OTel's own name (P6). `CallInfo.full_url` is always filled (E18).
- `stream()` drops `last_event_id=`, which `events()` keeps; `Stream.retry` becomes `Stream.reconnect_after`, a `float`
  in seconds like every other duration (P16). `aiter_bytes(*, chunk_size=)` is keyword-only (P26).
- `event_stream` becomes `as_events`, beside `as_bytes`, `as_json` and the new `as_text` (P19, T6).
- `CacheOutcome.SHARED` becomes `COALESCED`; `Partition` is a `StrEnum` (P20). `CacheResult` becomes `CacheChecked`
  and `Deprecated` becomes `DeprecationNoticed`, so every event is a past-tense fact (P27).
- `head()` requires `expect=` like every verb (P25): `head(url, expect=nothing)` is the one spelling.
- `HttpCache(methods=frozenset({"GET"}), statuses=frozenset({200}))`: defaults are values, so `None` keeps meaning
  off (P12). The two stay typed `Collection[str]` and `Collection[int]`, as 1.7 typed and checks them, so a tuple is
  still accepted; the fields hold frozensets, and `None` for either is a `TypeError`. (Amended with the phase 3 plan.)
- Time has one seam (P8): the `timing=` keyword goes from `RateBudget`, `RetryBudget`, `BreakerRegistry`,
  `CircuitBreaker` and `CachedTokenAuth`, and `FaultyTransport` drops `sleep=`. `timing_override()` and
  `TestClock.install()` are the way in, as they already are for `Client`.

**3.6 A class reports its one home (F5, F9).** Every exported class and function has `__module__` set to the public
module that exports it, and the one-home test checks it. The six top-level errors are defined privately and report
`headgate`; `from headgate.errors import ClientError` becomes an `ImportError`, and tracebacks print
`headgate.UpstreamStatusError`. The same applies to the testing kit (`headgate.testing.UnmatchedRequestError`).
`Paginated` becomes the concrete final class the walk is, so `await client.paginate(...)` names `Paginated`.
(F5's alternative, moving all fifteen errors to `headgate.errors`, re-opens design 8 and was not taken.)
The cost is that `inspect.getsource` (and IPython's `Client??`) cannot find a class's source: it looks the class up in
the module `__module__` names, where only the import is. Functions, and every traceback, keep their real file paths,
since those come from the code object. The public home wins because it is what a user reads and copies: a traceback, a
`repr`, `help()` and a pickle name the one import that works, and the private module is no second home. trio and numpy
pay the same cost for the same reason. (Amended with the phase 3 review.)

### 4. The surface is what users use

**4.1 Live objects hide their mechanism (P3, E10).**

- `RetryBudget`'s `record_first`, `try_spend` and `refund` become private, reached through a private accessor as the
  rate layer reaches a `RateBudget`'s pacer. Its surface is the constructor, `share` and its `repr`.
- `CircuitBreaker` leaves `headgate.breaker`; its constructor and `track` are private.
- `BreakerRegistry` keeps its constructor, `policy`, `max_hosts`, `len()`, `clear()` and `repr`. `breaker_for`,
  `breakers()` and iteration become private.
- `headgate.health.breaker_states(registry)` is the one reader. `BreakerSnapshot` becomes a frozen dataclass with
  `state`, `refusing` and `failures`.
- `RetryPolicy.wait_after` and `Backoff.delay` become private.

**4.2 No hidden global that repeats an event (P7, E16).** `health.cache_degradations()` and its counter are removed;
the `CacheChecked` event with `outcome=DEGRADED` reports each one with its call. `observer_failures()` stays and says it
is process-wide: an observer that raises cannot be reported through observers. (E16's per-client counter was not
taken: an application reads health from one place without holding every client.)

**4.3 One module per idea (P9).** `policy_disabled()` moves to `headgate.testing`; `headgate.policy` is removed.

**4.4 The SSRF guard is configured once (P10).** `SecurityOptions(check=)` joins `resolver=`, accepted only with the
guard on. `AddressGuard`, `PinnedHTTPTransport`, `resolve_with_loop` and `AsyncCurlTransport.bind_guard` become
private; `forbidden_reason` stays, so a custom check can defer to the default. (E9's alternative, publishing the
pinning protocol for third-party transports, was not taken: the one transport that needs it ships in the package.)

**4.5 Internal predicates and helpers go private (P13, P21, P22).** `HttpCache.caches_request` and `stores_status`;
`headgate.pagination.parse_link_header` and `Link`; `headgate.testing.Respond.response`.

**4.6 Types in public signatures are public (E11).** `CallUrl` is exported beside `Method`; `RawContent` and
`UploadFiles` beside `Raw` and `Multipart`.

**4.7 The fakes agree (P23).** `FaultyTransport.calls` is a read-only tuple property, as `FakeTransport.calls` is.

### 5. Failures where they are made

**5.1 Kind checks (F8, T12).** Every input in T12's table raises `TypeError` naming the option at construction or at
the call: `params=` and `headers=` given pairs, non-`str` header values, `idempotency_key`, `last_event_id`,
`Timeouts` fields, `Expect.on`'s status, `RateBudget`'s rate, `QueryParamAuth`'s mapping, `paginate`'s pager, an
unknown `stream(method=)` (and lowercase methods), a `bool` where a number is wanted, retry statuses outside 100–599,
and observers without a callable `notify`.

**5.2 Bodies fail at construction (F6, T17).** `Json` encodes its value when built and refuses a value of non-builtins
with a message pointing to a flavor's `writer(Type)` or `to_builtins`.

**5.3 Readers (T6, T7, T14).** `as_text: Reader[str]` decodes with the response's charset. `Reader` refuses a class
(`Reader(str)`), pointing to `as_text` and `Reader(lambda r: Cls(r))`. `headgate.msgspec`'s codec refuses at
`decoder()` a type msgspec reports as a custom type (a pydantic model, a plain class), naming the flavor to use, and
`check_codec` asserts that a codec refuses an unsupported type when the decoder is built. A `refuse=` that returns
neither a `RefusedError` nor `None` is a `TypeError` naming `refuse=`, outside the shape errors; `RefusedError(kind=)`
must be an `UpstreamStatusError` subclass, checked when built. (`TypeError` stays a shape error: a decode function's
`Model(**data)` raises it for a body of the wrong shape.)

**5.4 Declarations that cannot happen (T10, T18).** An `Expect` that declares a redirect status on a call that follows
redirects raises `ValueError` saying to pass `follow_redirects=False`. Refused at construction: an empty or blank
bearer token, one that already starts with `Bearer `, an empty `StaticHeaderAuth` or `QueryParamAuth` mapping, a
negative `refresh_margin`; at the call, `cache_ttl=` on a client without a cache.

**5.5 Every pager gets the loop guard (E17).** The repeated-request check runs for every pager whose page had items,
not only for the three built-in offset strategies, and the `isinstance` on concrete classes is removed.

### 6. Testing an SDK through its front door

**6.1 `fake_network` reports what it built (F3, E4).** `with fake_network(fake) as net:` yields a handle whose
`.transport` is the fake and whose `.clients` are the `Client`s built in the block, so `budget_for`, `cache_of` and
`breakers_of` are called without touching an SDK's private attribute.

**6.2 `fake_network` resolves names (E3).** Under `fake_network`, the SSRF guard's resolver answers every name with one
fixed, globally routable address; `FakeTransport.resolve(host, *addresses)` sets a host's answer, so a test can
exercise a refusal. The guard's checks keep running; a private IP literal is still refused.

### 7. Docs that run

- The cattrs/orjson codec in `docs/codecs.md` registers strict hooks, imports `TypeForm`, and passes `check_codec`
  (E5). The `Codec` protocol's docs state that a codec handles built-in containers, which `allow_empty_body` and
  `check_codec` rely on (E15).
- Every doc drift F10 and E19 list is fixed, and the lambda pager on `docs/index.md` follows 3.2.
- A docs test extracts and runs (or type-checks) every fenced Python block in `README.md` and `docs/` that is marked
  runnable, so a removed spelling cannot survive in the docs. Left to the builder: the marker and which blocks need a
  fake network.

## Dropped findings

- **T15** (`status_code: int | None` on every upstream error): design 6 decided it.
- **E9** (a public pinning-transport protocol) and **E16**'s per-client failure counter: see 4.4 and 4.2.
- **T14**'s removal of `TypeError` from the shape errors: see 5.3.
- **F5**'s move of all errors to `headgate.errors`: see 3.6.

## Migration

One breaking minor release, 0.13.0, after the last phase. No aliases. Each phase lands as its own pull request, with
its tests, docs, snapshot update and `CHANGELOG.md` entries under **Unreleased**, each saying what to write instead,
and leaves `main` green.

## Phases

Each phase is stacked on the one before, since all of them touch `_client.py`, the snapshot and the changelog.

1. **Safety at the seams:** section 1.
2. **Auth, one way:** section 2.
3. **One name per concept:** section 3.
4. **The surface:** section 4.
5. **Failures, testing and docs:** sections 5, 6 and 7.

## Left to the builder

- Whether `headgate.Auth` is a base class or a protocol (1.1).
- Internal structure, private names, the exact wording of every message (which names the option, what it takes and
  what it got), and test layout, as long as each changed behavior has a test that fails without the change.
