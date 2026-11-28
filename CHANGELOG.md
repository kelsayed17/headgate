# Changelog

All notable changes to headgate are recorded here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html). Before 1.0, a minor release may
change the public API; such changes are listed under **Changed** and **Removed** with what to write instead.

## [Unreleased]

### Added

- headgate is tested on free-threaded Python (3.14t), where it keeps the GIL off, and declares it
  (`Programming Language :: Python :: Free Threading :: 3 - Stable`). A `Client` belongs to one event loop, so build
  one per loop or thread; a `RateBudget`, `RetryBudget` or `BreakerRegistry` shared by clients across threads keeps
  its state under a lock, on either build (see the configuration guide's threads and event loops).
- `headgate.Auth`, the one type `Client(auth=)` takes: an `httpx2.Auth` that declares the headers and query parameters
  it stamps (`credential_headers`, `credential_params`, each a `frozenset` of names, as class attributes or
  properties) and names its credential (`credential_identity()`). A subclass that leaves one out cannot be built.
  `BearerAuth`, `StaticHeaderAuth`, `QueryParamAuth`, `SigningAuth` and `CachedTokenAuth` all derive from it. It is
  `headgate.security.CredentialStamper` made a base class: an `Auth` is a `CredentialStamper` by its shape, which a
  type check pins, and the protocol remains for a middleware that has a credential identity of its own. `Auth` does
  not derive from the protocol, so `Auth()`, `SigningAuth()` and `CachedTokenAuth()` read as taking nothing
  (`inspect.signature`, `help()`), and a base listed after `Auth`, such as `httpx2.DigestAuth` or a cooperative
  mixin, keeps its own constructor.
- `RouteOptions` takes `timeout=`, as every other layer of overrides does: `Route("/exports",
  timeout=Timeouts(read=120.0))` sets the phases it names on its paths, under a view's and a call's own timeouts and
  over the policy's. A route timeout longer than the deadline its paths run under (its own, else the policy's) raises
  `ValueError` where it is given.
- `as_text`, a `Reader[str]` beside `as_bytes`: the body as text, decoded by the response's `charset`, as
  `Stream.aiter_lines` decodes it (UTF-8 without one, or for one such as `base64` that names no text encoding, where
  `response.text` raises). A leading UTF-8 byte order mark is dropped, and the rest read as UTF-8. A body its
  `charset` cannot decode at all (UTF-16 without a byte order mark) raises `ResponseParseError`. Write
  `expect=as_text` for a plain-text body.
- `SecurityOptions(check=)`, beside `resolver=`: the function that judges each address the SSRF guard resolves,
  returning why it refuses one or `None` to admit it, on every hop and over every transport. It defaults to
  `headgate.security.forbidden_reason` itself, which a check of your own can defer to for the addresses it does not
  decide. A check of your own is accepted only with `block_private_hosts=True` (`ValueError` otherwise, as for
  `resolver=`), and `check=None` is a `TypeError` naming the default: leave it out. A cached response is served
  without its address being vetted again, so a guarded client keeps cache entries apart by its check and resolver: a
  client whose check refuses a host is never served what a client with a laxer check fetched from it, even once that
  client and its check are gone. Guards are told apart by their objects, a bound method by its object and its function
  (`resolver=dns.resolve` given to two clients is one resolver), so clients share entries when they are given the same
  check and resolver objects, and two equal but separate ones keep apart: build each once, and pass that object.
- `headgate.CallUrl`, `headgate.RawContent`, `headgate.UploadFiles` and `headgate.UploadFile`, the types of a verb's
  URL, of `Raw(content)`, of `Multipart(files)` and of one file in it, which their signatures named but nothing
  exported: a wrapper writes
  `async def fetch(self, url: CallUrl, **options: Unpack[CallOptions])`. `CallUrl` is a plain union, so a type
  checker that refuses a computed `str` still names `LiteralString` in its message.
- `CacheOutcome.UNSTORED` (value `"unstored"`): a call accepted the network's answer, but the cache could not store
  it, because the storage failed while writing it or the write outlasted the call's deadline. It is a second
  `CacheChecked` on that call, after its `MISS`, or its `COALESCED` when it waited on the download and was the first
  to accept it, where the fault was only counted. `DEGRADED` keeps its one meaning, a lookup that found the storage
  broken. An observer that took one `CacheChecked` per call as given may now see two; one that matches every outcome
  adds a case for it.
- `headgate.testing.FakeNetwork`, what `fake_network` yields: `net.transport`, the transport the clients built in the
  block send on, and `net.clients`, those clients in order. Only `fake_network` builds one: `FakeNetwork(...)` raises
  `TypeError`. A test reads an SDK's configuration back through them,
  `budget_for(net.clients[0])`, without reaching into the SDK's private `Client`.
- A name imported from the wrong public module is refused naming its home, where it raised the bare `cannot import
  name`: `from headgate.retry import RetryPolicy` raises `ImportError: RetryPolicy lives in headgate: from headgate
  import RetryPolicy`, and a name two modules export names both (`reader lives in headgate.msgspec and
  headgate.pydantic`). Asked as an attribute (`headgate.retry.RetryPolicy`), the same message is the `AttributeError` a
  missing attribute raises, so `hasattr()` answers `False` and `getattr()` with a default returns it, as before. The
  homes are written out, so a miss imports nothing. Type checkers see no module `__getattr__`, so they still report
  the import.
- A `FakeTransport` route written as an absolute URL, `reply("POST", "https://auth.example.com/oauth/token", ...)`,
  matches that origin's path, before a route of the bare path, which matches it on any host: a token host and the API
  host answer apart, and an SDK that sends a token request to the wrong host matches nothing. `calls_to()` takes the
  same two forms. Such a route matched nothing.
- `FakeNetwork.resolve(host, *addresses)`: the SSRF guard of every client built in the block resolves `host` to
  `addresses`, so a test can exercise a refusal (`net.resolve("api.example.com", "10.0.0.1")`) or a name that does not
  resolve (`net.resolve("api.example.com")`). Names are the network's, so it holds whatever transport the block was
  given, a wrapper of a `FakeTransport` included. A host is matched in any of its spellings, as the guard looks it up:
  in any case, a Unicode name as its IDNA form, with or without one final dot (so `localhost.` resolves to loopback,
  as `localhost` does); a host IDNA refuses raises `ValueError`. Each address is an IP address as text; anything else
  raises naming it.
- The docs have an imports page, every public name and the module it is imported from, generated from each module's
  `__all__` and held to it by a test.

### Changed

- `Client(auth=)` takes a `headgate.Auth`. A plain `httpx2.Auth`, `httpx2.BasicAuth` and `httpx2.DigestAuth` included,
  raises `TypeError` when the client is built, since it declares nothing the guard could withhold from another origin:
  subclass `headgate.Auth` and declare what it stamps, or write `StaticHeaderAuth({"Authorization": ...})` for a fixed
  header such as basic auth. Digest auth is `class Digest(Auth, httpx2.DigestAuth)` with the two declarations and a
  `credential_identity()` (see the auth guide). A virtual subclass made with `Auth.register` is refused too, since
  registering declares nothing.
- `get()` and `head()` take no `body=` (their options are `CallOptions`): a GET's body has no meaning, and the cache
  keyed the entry by its URL alone, so a second GET with another body was served the first one's answer. A body keyword
  on either raises `TypeError` pointing to `query()`, the safe read with a body: write
  `query(url, expect=..., body=Json(...))`. `delete()` keeps `body=`. A `stream()` with `method="GET"` or
  `"HEAD"` refuses `body=` the same way: write `stream(url, method="QUERY", body=...)`. `events()` takes no body
  keyword at all: its options are `headgate.streaming.EventOptions`, a stream's less the body, so `body=` is a type
  error, and raises `TypeError` saying `events()` opens a GET: for an event stream a POST opens, write
  `stream(url, method="POST", body=...)` and read its `aiter_events()`.
- A call's `httpx2.URL` must be absolute. A relative one raises `TypeError`, since its only use was to skip escaping:
  `httpx2.URL(f"items/{uid}")` type-checked and reached `/admin` for `uid = "../admin"`. Write a t-string,
  `t"items/{uid}"`, or `{fragment:raw}` for a pre-built fragment. A URL of any other kind (`None`, `bytes`, a list, a
  number) raises `TypeError` too, instead of being sent as its text with the client's credentials: pass a `str`, a
  t-string or an absolute `httpx2.URL`.
- `params=`, `Form` fields and `Multipart` fields share one allowlist of values with a wire meaning:
  - a `str`, `int`, `float`, `bool` or `None` is sent as before;
  - an `Enum` member is sent as its `.value`, which must be a `str` or an `int`: a plain `Enum` went out as
    `Status.OPEN`, and now goes out as `open`;
  - a `UUID` or a `Decimal` is sent as its text, as before;
  - a `date`, `datetime` or `time` is sent in ISO 8601, `2026-01-02T03:04:00+00:00`, where `str(datetime)` put a
    space in place of the `T`;
  - every other value raises `TypeError` naming the key and its type, instead of being sent as its Python `repr`: a
    dataclass, a msgspec `Struct`, a pydantic model, `object()`, a mapping, a set, `bytes` (but in a `Multipart`
    field, which sends it as it is), an `Enum` whose value is neither a `str` nor an `int`, and a list or tuple inside
    a list or tuple. `params=` refuses it at the call, and a
    `Form` or `Multipart` when it is built (a nested `dict` in a `Form` was sent as its `repr`). Encode such a value
    into text first, with `json.dumps` or the API's own format.
- `Form.fields` and `Multipart.fields` hold the encoded values, a copy of the mapping given: `Form({"s": Status.OPEN})`
  has `fields == {"s": "open"}`. Read the values you built the body from, not the body's.
- Parameters that take names or codes take a collection of them: `CassetteFilter(headers=, params=)`,
  `HttpCache(methods=, paths=, statuses=)`, `RetryPolicy(methods=, statuses=)` and `SecurityOptions(trusted_origins=)`
  are typed `Collection[...]`, so a type checker takes the tuple, list or set a refusal suggests, as the runtime did
  (`RetryPolicy` and `SecurityOptions` were typed `frozenset[...]`). The ones whose order decides which entry wins are
  typed `Sequence[...]`: `HttpOptions(request_id_headers=)` and `CallPolicy(routes=)`, typed `tuple[...]` before, so a
  list type-checks too; and `FaultyTransport(schedule=)` takes any `Iterable` of steps, where `list[Step]` refused a
  `list[Respond]`. What each holds is unchanged, a `frozenset` of the names or codes and a `tuple` of a sequence, but
  the fields read as the type they take: write `frozenset(policy.methods) | {"POST"}` for set algebra on one, or build
  the policy from `IDEMPOTENT_METHODS | {"POST"}`. A bare `str` (read one letter at a time), a number or a one-shot
  iterator (a generator, which checking would empty) raises `TypeError` naming the option: write `methods=("GET",)` or
  `statuses=(503,)`, and turn a generator into a tuple. `RetryPolicy(methods="GET")` retried no GET, and
  `HttpCache(methods="GET")` raised a `ValueError` that named the letters.
- `SigningAuth` and `CachedTokenAuth` declare what they stamp in `credential_headers` and `credential_params`, the two
  names every `Auth` and middleware declares with. `signed_headers` and `signed_params`, `token_headers` and
  `token_params`, and the properties that mapped them onto `credential_*`, are removed: write
  `credential_headers = frozenset({"X-Signature"})` where you wrote `signed_headers = ...` or `token_headers = ...`.
  `CachedTokenAuth` declares `frozenset({"authorization"})` by default. The names are reported as declared, no longer
  lower-cased (the guard, the cache and `CassetteFilter` compare them case-insensitively). A `SigningAuth` that
  declares no name, and a `CachedTokenAuth` that overrides `attach` without declaring again what it sets, still fail
  when the class is defined. Both declare in class attributes: a `credential_headers` or `credential_params` property,
  which `CachedTokenAuth` accepted in place of `token_headers`, raises `TypeError` when the class is defined. For names
  that differ per instance, write a subclass per set of names, each with its own class attribute, or a plain `Auth`,
  which may still declare through a property.
- `CachedTokenAuth` mints through one abstract hook, `mint(request)`: an async generator that yields each request the
  exchange needs and ends by yielding `Minted(token, lifetime)`. It is `mint_flow` renamed: write
  `async def mint(self, request)` where you wrote `async def mint_flow(self, request)`. A mint that makes no request of
  its own yields only the `Minted`: where `mint_token()` returned `(token, lifetime)`, write
  `async def mint(self, request): yield Minted(token, lifetime)`. A subclass without a `mint` raises `TypeError` when
  it is built, naming `mint`, instead of when it is defined. A `mint` that is not an async generator function (a
  coroutine that does `return Minted(...)`, a sync generator) raises `TypeError` when the class is defined, showing that
  spelling, where the first call died with `'coroutine' object has no attribute 'aclose'`.
- `CachedTokenAuth.seed()` takes the shape a mint ends with: write `auth.seed(Minted(token, lifetime))` for
  `auth.seed(token, lifetime)`.
- `CachedTokenAuth` has no constructor: its cache and lock are made on first use, so a subclass whose `__init__`
  forgets `super().__init__()` works, where it failed at its first call with an `AttributeError` naming private state.
  `super().__init__()` may stay, and does nothing. `refresh_margin` is a class attribute, a finite number of seconds of
  0 or more checked when the class is defined (a `TypeError` for one that is not a number, a `ValueError` for a
  negative, NaN or infinite one), and the class's value is the one read, not an instance's: write
  `refresh_margin = 30.0` in the class body for `super().__init__(refresh_margin=30.0)`. `CachedTokenAuth(timing=)` is
  removed: a token's lifetime runs on the timing of the clients built with the auth, so build the client inside
  `timing_override()` or `TestClock.install()` (building the auth there alone does nothing). Its clients share one
  timing: a client built on another timing than an earlier one's, or than the one a token was seeded or minted on,
  raises `TypeError` before it builds anything, leaving the auth as it was, so build them, and seed the auth, inside
  one block. A copy of an auth (`copy.copy`) starts with a cache, lock and identity of its own, empty, rather than
  sharing the original's.
- `async_auth_flow` is `@final` on `SigningAuth` and `CachedTokenAuth`, and so is `reject_credential_rotation`: a
  subclass that overrides one raises `TypeError` when it is defined, and ty reports `override-of-final-method`. An
  override of the flow type-checked and failed inside the call (`'NoneType' object has no attribute 'status_code'`).
  Their hooks are `sign` and `skips`, and `mint`, `attach` and `skips`; add a per-request header, such as a tenant id,
  in a middleware. A class of both bases, `class Both(CachedTokenAuth, SigningAuth)`, raises `TypeError` saying the
  two cannot be combined in one auth, since each runs a flow of its own, where it blamed an override of the flow the
  class never wrote: subclass `CachedTokenAuth` for the token, and sign each attempt in a middleware, which sees the
  token on the request. headgate tells the two bases apart by a class's own bases, not `isinstance`, so the refused
  class, which lingers among its bases' subclasses until it is collected, cannot make a later signer read as a token
  auth, or the reverse.
- `SigningAuth.sign(request, now)` is handed the attempt's wall-clock time in Unix seconds, from the client's timing, so
  a signer whose client is built under `TestClock.install()` runs on the test clock like the rest of the client. Write
  `def sign(self, request, now)` and sign with `now` where you called `time.time()`.
- A hook of the wrong color is refused where it is given, as ty already reported it: a `SigningAuth` whose `sign` is
  a coroutine function raises `TypeError` when the class is defined (its requests went out unsigned, with only a
  "never awaited" warning): write `def sign(self, request, now)`. `Client()` raises `TypeError` for an observer whose
  `notify` is a coroutine function (its events were dropped): write `def notify(self, event)`; and for a middleware
  whose `__call__` is not one (it failed at the first call): write `async def __call__(self, request, call_next)`. A
  coroutine-function `skips`, on either auth base, and `attach` on a `CachedTokenAuth`, are refused when the class is
  defined too: an `async def skips` returned a coroutine, which is truthy, so every request went out unsigned or
  without its token. Write `def skips(self, request)` and `def attach(self, request, token)`.
- `Minted(token, lifetime)` refuses, when it is built, a lifetime that is not a number of seconds above 0, where a
  mint or a `seed()` cached a token already due: a `TypeError` for one that is not a number (`True`, a `str`), a
  `ValueError` for one not above 0 (`0`, a negative number, NaN). `math.inf` is a token that never expires. Pass the
  provider's `expires_in` as a number.
- `CassetteFilter(auths=)` takes auths and middleware (`Iterable[CredentialCarrier]`). An entry that declares no
  `credential_headers` and `credential_params`, which it skipped, raises `TypeError`: pass only auths and middleware,
  and name any other credential in `headers=` or `params=`. A middleware's declared names are now filtered too.
- A walk's body is its `payload`, so "query" names only the QUERY verb and, in prose, the URL query:
  `paginate(payload=)`, `PageRequest.payload` and `PageRequest.with_payload()`. Write
  `paginate(url, payload=Query(...), ...)` for `paginate(url, query=Query(...), ...)`, and `request.payload` and
  `request.with_payload(...)` in a pager for `request.body` and `request.with_body(...)`. `paginate(query=)` raises
  `TypeError` naming `payload=`.
- A pager carries its page reader, as its first argument, so a type checker knows the page type before it reads the
  lambdas after it: `Cursor(reader(Page), items=lambda p: p.items, next=lambda p: p.next, param="cursor")` is a
  `Cursor[Item, Page]`, and `lambda p: p.itemz` is a type error where it was an `Unknown` that reached every item.
  `paginate()` takes no `expect=` and names the pager `pager=`, not `pages=` (which `Paginated.pages()` uses for the
  pages): write `paginate(url, pager=LinkHeader(reader(list[Issue]), items=lambda page: page))` for
  `paginate(url, expect=reader(list[Issue]), pages=LinkHeader(items=lambda page: page))`. The reader may be an
  `Expect`, so declared answers, `accept=` and `headers=` reach every page as before. Every pager's generic parameters
  read item first, as `Paginated[I, P]` does: `Pager[I, P, B]`, `WritingPager[I, P, B]`, `Cursor[Item, Page, Query]`
  for `Cursor[Page, Item, Query]`, and the reader and writer usually bind them, so the subscript can go. The `Pager`
  protocol gains a `read` property: give a pager of your own a `read`, such as a dataclass field `read: Reader[Page]`.
  `WritingPager` declares `read` and `write` for its subclass to give, as fields or properties, rather than making
  `write` an abstract property, and no longer subclasses the `Pager` protocol (it is one by its shape), so a plain
  `@dataclass(frozen=True)` subclass with `read` and `write` fields builds: write `read: Reader[Page]` and
  `write: Writer[Query]` as fields, where a `write` property was required. Its methods stay abstract. `paginate()`
  raises `TypeError`, before the pager runs, for a pager whose `read` is missing or is not a `Reader` or an `Expect`,
  and for a writing pager with no `write`.
- `Paginated` is the class of the walk `paginate()` returns, a final class rather than a protocol (a subclass raises
  `TypeError` where it is defined), so a mistake names
  it: `await client.paginate(...)` raises `TypeError: 'Paginated' object can't be awaited`, where it named a private
  `PageWalk`. Iterate with `async for`, or `await walk.collect()`. Annotations are unchanged: `Paginated[Issue]` and
  `Paginated[Issue, IssuePage]`. A test double that stood in for a walk by the protocol's shape (a class with
  `__aiter__`, `pages()` and `collect()`) no longer type-checks as one: build the real class from its pages, write
  `Paginated(pages, lambda page: page.items)`, where `pages` is a function of no arguments that starts the pages afresh
  (see the testing guide's "A fake walk").
- Breakers take the shape a rate does: a policy's `breaker=` is a `BreakerPolicy`, from which the client builds
  breakers of its own, or a shared `BreakerRegistry`, built from one, whose breakers every client given it shares
  (`breaker: BreakerPolicy | BreakerRegistry | None`, as `rate: Rate | RateBudget | None`). The registry holds the
  thresholds, given first as a `RateBudget`'s rate is: `BreakerRegistry(BreakerPolicy(failure_threshold=3),
  max_hosts=256)`, read back as `.policy`, and `BreakerPolicy()` when left out. Write
  `breaker=BreakerRegistry(BreakerPolicy(failure_threshold=3))` for
  `breaker=BreakerPolicy(failure_threshold=3, registry=BreakerRegistry())`. The lookup that took thresholds,
  `registry.breaker_for(origin, failure_threshold=..., recovery_timeout=...)`, is gone with the lookup itself (see
  **Removed**).
- `TransportPolicy` is `CallPolicy`, the pair of `CallOptions`, which override it per call, leaving "transport" to
  the network layer (`HttpOptions(transport=)`, `TransportError`): write `Client(policy=CallPolicy(...))` for
  `Client(policy=TransportPolicy(...))`. It holds the client's rate, beside the routes that override it per path:
  `Client(rate=)` is removed, so write `Client(base_url=..., policy=CallPolicy(rate=Rate.per_second(10)))` for
  `Client(base_url=..., rate=Rate.per_second(10))`. A rate of the wrong kind raises `TypeError` from `CallPolicy`, and
  `policy_disabled()` still builds a client's budget unpaced.
- `Expect` declares its answers one way, with `.on()`, which widens the result type with each: `answers` is no longer
  a constructor argument, and stays readable. Write `Expect(reader(Order)).on(404, nothing)` for
  `Expect(reader(Order), {404: nothing})`. `Expect` is no longer a dataclass, so no copy drops the answers `.on()` declared:
  `copy.replace(expect, accept=...)` and `copy.copy(expect)` keep them, and `dataclasses.replace(expect, ...)`, which
  built one without them, raises `TypeError`; write `copy.replace` for `dataclasses.replace`. Its `ok`, `answers`,
  `headers` and `accept` are read-only properties, and two equal `Expect` objects hash alike. Its `repr` reads as it
  is built, `Expect(Reader(as_json), headers={}, accept=None).on(404, Reader(nothing))`, with the answers by status,
  rather than spelling an `answers=` the constructor refuses.
- `head()` requires `expect=`, as every verb does: write `head(url, expect=nothing)` for `head(url)`.
- A middleware reads its call with `headgate.call_view(request)`, which returns a `CallView`: write `call_view` for
  `call_info`. Its `call` is the call's `CallInfo`, as every event's `call` is. `CallInfo.route`, the t-string
  template, is `CallInfo.template`, OpenTelemetry's `url.template`, since a `Route` is the per-path rule: write
  `call_view(request).call.template` for `call_info(request).call.route`, and `event.call.template` for
  `event.call.route`.
- `stream()` takes no `last_event_id=`, which `events()` keeps as the one way to resume a server-sent event stream:
  write `events(url, last_event_id=...)` for `stream(url, last_event_id=...)`, or send `Last-Event-ID` in a raw
  stream's `headers=`. `Stream.retry`, the reconnection time in milliseconds, is `Stream.reconnect_after`, in seconds
  like every other duration: write `await asyncio.sleep(stream.reconnect_after)` for
  `await asyncio.sleep(stream.retry / 1000)`. `Stream.aiter_bytes()` takes `chunk_size=` by keyword only, as the
  other sizes are: write `aiter_bytes(chunk_size=1024)` for `aiter_bytes(1024)`.
- `event_stream` is `as_events`, a reader beside `as_bytes`, `as_json` and `as_text`, where its name said it was a
  stream: write `expect=as_events` for `expect=event_stream`. `events()` still opens a stream.
- Every event's name is a past-tense fact: `CacheResult` is `CacheChecked` and `Deprecated` is `DeprecationNoticed`,
  so write `case CacheChecked(outcome=...)` and `isinstance(event, DeprecationNoticed)`. `CacheOutcome.SHARED`, a
  call that waited on an identical request's download, is `CacheOutcome.COALESCED` (value `"coalesced"`), leaving
  "shared" to `Partition.SHARED`. `Partition` is a `StrEnum`, as `BreakerState` and `CacheOutcome` are, so
  `str(Partition.SHARED)` is `"shared"`.
- `HttpCache(methods=, statuses=)` default to the values themselves, `frozenset({"GET"})` and `frozenset({200})`, and
  hold them, where `None` stood for them; `None` for either raises `TypeError`, since `None` means off and the cache's
  off is `cache=None`. Leave them out for the defaults. `paths=None` is still the path filter off.
- Time has one seam, `timing_override()` (and `TestClock.install()`, built on it), as it already was for `Client`:
  `RateBudget`, `RetryBudget` and `BreakerRegistry` take no `timing=`, and `FaultyTransport` no `sleep=`. Each runs
  on the timing current when it is built, the breakers a registry builds later included. Write
  `with clock.install(): shared = RateBudget(rate)` for `RateBudget(rate, timing=clock.timing)`, and
  `with clock.install(): network = FaultyTransport(schedule=[...])` for
  `FaultyTransport(schedule=[...], sleep=clock.sleep)`.
- Every exported class and function reports, in `__module__`, the public module it is imported from, so a traceback,
  a `repr` and `help()` name the one import: `headgate.UpstreamStatusError`, `headgate.testing.UnmatchedRequestError`
  and `headgate.Client`, where they named `headgate.errors.UpstreamStatusError`,
  `headgate.testing._transport.UnmatchedRequestError` and `headgate._client.Client`. Pickles name the same homes.
  Code that compared `__module__` to a private module's name compares it to the public one. A cache partition keyed
  by an auth's class name is keyed by its public name now, so an entry a credential-partitioned cache stored before
  is not found again, and is fetched afresh.
- The six top-level errors (`ClientError`, `TransportError`, `ClientTimeoutError`, `UpstreamStatusError`,
  `AuthError` and `RateLimitError`) have one home, `headgate`: `from headgate.errors import ClientError` raises
  `ImportError`, so write `from headgate import ClientError`. `headgate.errors` keeps every other error,
  `ErrorDetails` and `Problem`. A pickle of one of the six made before this release names it as
  `headgate.errors.ClientError` (and so on), which no longer resolves, so it no longer loads: unpickling it raises
  `AttributeError`. Re-create such pickles from the error, or load them with the earlier release.
- A public module binds only what it exports and its public submodules, so no name has a second home:
  `headgate.health`, `headgate.msgspec`, `headgate.pydantic` and `headgate.otel` re-export what private modules
  define, as every other public module does, and the names they imported to do their work are gone from them. Import
  each from its own home: `from headgate.breaker import BreakerState` for `from headgate.health import BreakerState`,
  `import msgspec` for `headgate.msgspec.msgspec`, and `from headgate import reader_for` for
  `headgate.msgspec.reader_for`.
- `HttpCache(methods=(), paths=(), statuses=())`, an empty rule that made a cache store nothing, raises `ValueError`
  naming `cache=None`: write `CallPolicy(cache=None)` for a client without a cache.
- `policy_disabled()` lives in the testing kit, beside `instant_waits()`, and `headgate.policy`, a module for one
  function, is removed: write `from headgate.testing import policy_disabled` for
  `from headgate.policy import policy_disabled`. It reports `headgate.testing` as its home.
- `headgate.testing.instant_retries()` is `instant_waits()`, named for what it does: every wait of the clients built
  in its block returns at once, a rate budget's pacing included, not only a retry's backoff, and now a server's pause
  too, which it waited out on the real clock (see **Fixed**). Write `instant_waits()` for `instant_retries()`; a fake
  network alone still waits on the real clock.
- `TestClock.now` and `TestClock.epoch` are read-only properties, and `TestClock.sleeps` returns a new list each time it
  is read, where all three were plain attributes: an assignment moved the clock backwards, or to a value that is not a
  number, past the checks `advance()` makes, and a test could edit the record of waits. Write `clock.advance(seconds)`
  for `clock.now += seconds`, build a `TestClock(epoch=...)` for another epoch, and read `clock.sleeps` again to see a
  later wait. `clock.sleeps.clear()` now clears a copy and leaves the record as it was: note `len(clock.sleeps)`
  before the waits a test is about, and read from there, `clock.sleeps[start:]`.
- `FaultyTransport.calls` is a read-only property returning a tuple, as `FakeTransport.calls` is, where it was a
  list a test could append to or clear under the transport. Read it as before; write `list(network.calls)` where a
  test needs a list, and build a new `FaultyTransport` where it cleared the log.
- Every exported class is final, to a type checker (`typing.final`, so ty reports a subclass) and at runtime, where
  defining a subclass raises `TypeError` saying what to write instead, unless a guide names it a base to extend: the
  auths (`Auth`, `SigningAuth`, `CachedTokenAuth`, `StaticHeaderAuth`, `BearerAuth`, `QueryParamAuth`), `WritingPager`
  and the pagination strategies, whose hooks a walk calls, the flavors' codecs (`MsgspecCodec`, `PydanticCodec`), the
  `ClientError` tree, and `CallEvent`, which headgate's own events subclass. headgate reads a final class by its
  fields or holds it as a handle, so a subclass's override was accepted and never ran. Newly final: the option values
  (`Timeouts`, `CallPolicy`, `HttpOptions`, `SecurityOptions`, `IdempotencyKeys`, `BreakerPolicy`, `Rate`,
  `Adaptive`, `Hedge`, `RetryShare`, `Timing`), the shared handles (`RateBudget`, `BreakerRegistry`), `EncodedBody`,
  `Minted`, `PageRequest`, what headgate hands out (`Fresh`, `NotModified`, `Validator`, `Problem`, the events,
  `CallInfo`, `CallView`, `BreakerSnapshot`), `DecodeError` and `RefusedError`, `AsyncCurlTransport`,
  `OpenTelemetryObserver`, and the testing kit (`FakeTransport`, `FaultyTransport`, its steps and `FaultCall`,
  `CassetteFilter`, `TestClock`, `UnmatchedRequestError`). `Expect`, `Form`, `Multipart`, `Raw`, `Reader`, `Route`,
  `RetryBudget`, `Paginated` and `FakeNetwork`, final to a type checker only, now refuse a subclass at runtime too. Hold
  one, build it with the fields you need, or wrap it: a transport or an observer of your own delegates to it. A
  change to each request a subclass of `AsyncCurlTransport` made belongs in a middleware: the SSRF guard does not pin
  a transport that wraps it, and only vets each hop before it is sent.
- `Backoff`, `RetryPolicy`, `HttpCache` and `headgate.testing.Respond` are final: a subclass raises `TypeError` when
  it is defined, naming the fields to set, and ty reports it. The methods a subclass could override (`delay`,
  `wait_after`, `caches_request`, `stores_status`, `response`) are no longer called by anything, so an override
  would have been accepted and silently ignored. Set the fields instead: `Backoff(factor=..., max_wait=...)`,
  `HttpCache(methods=..., paths=..., statuses=...)`, and for a reply a `Respond` cannot describe, a function in a
  `FakeTransport` route.
- `headgate.health.observer_failures()` keys each observer class by its module and qualified name,
  `"myapp.metrics.Exporter"`, where it used the qualified name alone, so two classes of one name in different modules
  shared a count. Write `observer_failures().get("myapp.metrics.Exporter", 0)` for
  `observer_failures().get("Exporter", 0)`, or `f"{cls.__module__}.{cls.__qualname__}"` for `cls.__qualname__`.
- `headgate.health.BreakerSnapshot` is a frozen dataclass with `state`, `refusing` and `failures` (the consecutive
  failures since the last success, which only the live breaker showed), built by keyword, rather than a named tuple:
  write `snapshot.state, snapshot.refusing` for `state, refusing = snapshot`, and compare a snapshot with
  `BreakerSnapshot(state=..., refusing=..., failures=...)` rather than a tuple. The reader that returns them,
  `breaker_states()`, is `breaker_snapshots()`, with no alias, so code written against 0.12.1 fails where it imports
  it rather than compare a snapshot to a `BreakerState`, which is always `False`: write
  `breaker_snapshots(registry)[origin].state == BreakerState.OPEN` for `breaker_states(registry)[origin] == ...`.
- A client given a shared object that holds time, built on another timing than the client, raises `TypeError` before
  it builds anything, as it does for a `CachedTokenAuth` that runs on another: a `RateBudget` (as its policy's or a
  route's `rate=`), a `BreakerRegistry` (as `breaker=`) or a `RetryBudget` (as the `budget=` of its policy's or a
  route's `RetryPolicy`). One quota, cooldown or retry window cannot run on two clocks. This catches a shared budget
  built outside `TestClock.install()` and given to a client built inside it, which paced the test on real time. Build
  the shared object inside the same block as its clients: `with clock.install(): shared = RateBudget(rate);
  client = Client(..., policy=CallPolicy(rate=shared))`. Under `policy_disabled()` a client ignores a shared budget,
  and is not refused.
- A call's options are checked for their kind before anything is sent, each a `TypeError` naming the option:
  `params=` must be a mapping with `str` keys (a list of pairs, httpx's habit, raised an `AttributeError`);
  `headers=`, on a call, a view (`with_options`, which now raises where the view is made), `HttpOptions` and an
  `Expect`, must map names to `str` values (pairs raised an `AttributeError`, `{"X-N": 1}` httpx2's own error at the
  call, and a `bytes` value, `{"X-N": b"1"}`, was accepted and sent); `idempotency_key=` must be a `str` (an `int` raised an `AttributeError`, and `None` is refused: leave the
  option out for no key); `events(last_event_id=)` must be a `str` or `None`; and `stream(method=)` must be one of
  `"GET"`, `"POST"`, `"PUT"`, `"PATCH"`, `"DELETE"`, `"HEAD"` and `"QUERY"`, where any other value, a lower-case
  `"get"` included, was sent as given (a `str` that is none of them raises `ValueError`). Write `params={"a": 1}`,
  `headers={"X-N": "1"}`, `idempotency_key=str(key)` and `method="GET"`.
- Every number a client is configured with is an `int` or a `float`, checked where it is given, a `TypeError` naming
  the field: a `bool`, which a type checker takes for an `int`, a `str`, a `Decimal` and a `Fraction` are refused, and
  a `float` where a count goes. This covers `Timeouts`' four phases, `Rate`, `RateBudget(max_in_flight=)`,
  `RetryPolicy(attempts=, max_retry_after=, min_attempt_time=)`, `Backoff`, `Hedge`, `RetryShare`, `Adaptive`,
  `BreakerPolicy`, `BreakerRegistry(max_hosts=)`, `HttpCache(ttl=)`, the pagers' `page_size` and `start`, and the
  testing kit's `DropMidBody(after_bytes=)`, `Respond(status_code=, retry_after=)`, `Slow(seconds=)` and
  `TestClock(start=, epoch=)`. `Timeouts(read=True)`, `Rate(True, 1)`, `RetryPolicy(attempts=2.5)`,
  `Timeouts(read=Decimal("5"))` and `Rate(Fraction(1, 2), 1)` were accepted, and `Timeouts(read="5")` raised a
  comparison error from inside the constructor: write `Timeouts(read=5.0)`, `Rate(1, 1)`, `RetryPolicy(attempts=3)`
  and `float(value)` for a `Decimal` or a `Fraction`. `DropMidBody(after_bytes=)` below 0 and a `Slow(seconds=)`
  below 0 or NaN raise `ValueError` where the step is built: `after_bytes=0` drops the connection before anything is
  written, and `Slow(seconds=math.inf)` is a server that never answers, cut by the read timeout.
- The testing kit checks its other fields where they are given, each a `TypeError` naming the field:
  `Respond(headers=)` must map names to `str` values (a list of pairs was accepted), `Slow(then=)` must be a `Respond`
  (`Slow(1, then=200)` was accepted and failed at the send), and `TestClock(random=)` must be callable
  (`TestClock(random=0.5)` was accepted and failed at the first backoff). Write `Respond(headers={"X-A": "1"})`,
  `Slow(1, then=Respond(200))` and `TestClock(random=lambda: 0.5)`.
- `RateBudget(rate)` takes a `Rate` (`RateBudget(5)` raised an `AttributeError`: write
  `RateBudget(Rate.per_second(5))`), and `adaptive=` an `Adaptive` or `None`.
- `RetryPolicy(statuses=)` and `HttpCache(statuses=)` hold `int` statuses from 100 to 599: a `bool` or a numeric
  string, which no response's status equals, is a `TypeError`, and `999` a `ValueError`, where
  `RetryPolicy(statuses=frozenset({"500"}))` was accepted and never matched. Write `frozenset({500})`. A
  `RetryPolicy` also refuses a 1xx with `ValueError`, since httpx2 never answers with one; a 2xx stays allowed, for an
  API that answers 202 while a job runs.
- `Expect.on(status, ...)` takes an `int`: `on("404", ...)` raised a comparison error and `on(True, ...)` a
  `ValueError` about status 1; both are a `TypeError` naming `status=`. Write `on(HTTPStatus.NOT_FOUND, ...)`.
- `Client(observers=)` refuses an entry with no callable `notify`, such as `print` or an observer whose method is
  misspelled, with a `TypeError` naming it: such an observer was accepted, and every event failed into
  `observer_failures()`. Write a class with `def notify(self, event)`.
- Every built-in pager checks its fields when it is built: `read` must be a `Reader` or an `Expect`
  (`Cursor(5, items=...)` was accepted, and the walk failed at its first page), each hook (`items`, `next`, `more`,
  `total`, `offset`, `put`, `write`) callable, `page_size` and `start` an `int`, `param` a `str` and `stop_on_empty`
  a `bool`, each a `TypeError` naming the field. `paginate(pager=)` refuses an object without callable `first`,
  `page_items` and `next_page`, naming the first it lacks, where `pager=None` raised an `AttributeError`. Pass the
  page reader first: `Cursor(reader(Page), items=..., next=..., param=...)`. A class as `write=` is refused there too,
  as `Reader` refuses one (`write=dict` failed at the first page with an `AttributeError`): write `writer(Query)` with
  a flavor's writer, or leave `write=` out for builtins. What a hook returns is checked where the walk takes it, a
  `TypeError` naming the hook, as a reader's `refuse=` is: an `items` that returns no sequence (a set or a dict's view
  is taken too; a generator, which `Offset` failed to count and `Cursor` never found empty, is not: return a list),
  or returns text, whose characters the walk yielded one by one as items, or a mapping, whose keys it yielded (return
  the list inside it, or its `.values()`), a pager's own
  `page_items` (a strategy subclass's override included, named `Class.page_items()`) that returns no iterable, a
  `total` that returns neither an `int` nor `None`, a `next` that returns neither a `str` (an `httpx2.URL` too, for
  `NextUrl`) nor `None`, a `BodyOffset` `offset` that returns no `int`, and a `write` that returns no
  `EncodedBody`, which failed inside the walk with an `AttributeError` or a comparison error, or sent an `int`
  cursor as its text. A `next` that reads a numeric cursor returns its text, `str(cursor)`, and a `BodyCursor`'s
  `put` converts it back for a body that holds a number, `put=lambda body, cursor: {**body, "after": int(cursor)}`.
- `BearerAuth(token)` refuses an empty or blank token with `ValueError`, where it sent `Authorization: Bearer ` (the
  unset environment variable); a token that is the scheme, `bearer` in any case, or starts with it and whitespace
  (`Bearer abc`, `bearer\tabc`), where it sent `Bearer Bearer abc`, while `bearertoken` is a token like any other; and
  a token with whitespace around it, which went into the header as it was. A token that is
  not a `str` is a `TypeError`. No message shows the token. Pass the token alone and stripped:
  `BearerAuth(token.strip())`.
- `StaticHeaderAuth` and `QueryParamAuth` take a mapping of names to `str` values with at least one entry. An empty
  mapping, which stamped nothing, raises `ValueError`: build a client with no key with `auth=None` for
  `StaticHeaderAuth({})`. A value of the wrong kind, a list of pairs, a bare `str` (`QueryParamAuth("key")` raised a
  `ValueError` about a dictionary update sequence) or a value that is not a `str`, raises `TypeError` naming the
  option.
- `Json(value)` encodes its value once, where it is built, and the call sends those bytes. A value that is not
  builtins, such as a struct, a dataclass, a pydantic model, a `date` or a `set`, raises `TypeError` there, naming its
  type and saying what to write, where it raised the standard library's `Object of type Order is not JSON
  serializable` from inside the call; `NaN`, the infinities and a value that holds itself raise `ValueError` there
  too, naming `Json`. Write `body=writer(Order)(order)` with a flavor's writer, or `body=Json(to_builtins(order))`. A
  change to the value after the `Json` is built is no longer sent: build the `Json` from the value you mean to send.
  `Json` is a final value class rather than a dataclass: `dataclasses.replace`, `fields` and `asdict` no longer apply
  to it, so write `copy.replace(body, value=...)` and `body.value`, a read-only property; it compares, hashes, prints,
  matches (`case Json(value)`), copies, deep-copies and pickles as before, each copy a `Json` built afresh from the
  value, and a subclass raises `TypeError`. Text holding a lone surrogate (`Json("\ud800")`), which UTF-8 cannot
  encode, raises `ValueError` naming the surrogate where the `Json` is built, where it raised a `UnicodeEncodeError`
  at the send.
- `Reader` refuses a class with `TypeError` where it is built: `Reader(str)` type-checked and returned
  `'<Response [200 OK]>'`. Write `expect=as_text` for `Reader(str)`, `as_bytes` for `Reader(bytes)`, `as_json` for
  `Reader(dict)` or `Reader(list)`, and `Reader(lambda r: Cls(r))` for a class that is built from a response.
  `body_reader(decode)` refuses the classes that misread every body the same way, where `body_reader(str)` returned
  the body's `repr` (`"b'...'"`), `body_reader(bool)` `True` for any body but an empty one, `body_reader(tuple)` its
  byte values and `body_reader(Order)` an `Order` holding the bytes: write `as_text`, `as_bytes` or `as_json` for a
  builtin with a reader of its own, and a flavor's `reader(Order)` for any other builtin (`bool`, `tuple`, `set`,
  `frozenset`), a model (a dataclass, a `NamedTuple`, a `TypedDict`, a msgspec `Struct`, a pydantic model, an attrs
  class) or a type expression. A class of your own that keeps such a builtin's constructor, `class Symbol(str)` or
  `class Rows(list)`, is refused alike: write `body_reader(lambda content: Symbol(content.decode()))`, or
  `json.loads(content)` for the containers. A class that is built from the bytes, such as `io.BytesIO`, `int`, an enum
  of bytes or a `str` of your own whose `__new__` decodes them, is taken as before.
- A codec's `decoder()` raises `TypeError` for a type it cannot decode, and `check_codec` asks it to, so a reader of
  such a type fails where it is built. `headgate.msgspec.reader(Model)` of a pydantic model, or of a plain class
  anywhere in the type (`list[Plain]`, `dict[str, Plain] | None`), built, and every 200 then raised
  `ResponseParseError` as an unreadable body; it names the class, and a pydantic model's flavor:
  write `headgate.pydantic.reader(Model)`. A writer is refused the same way, where it is built:
  `headgate.msgspec.writer(Plain)` was accepted and failed at the first call, and now raises `TypeError` saying it
  cannot encode the class, pointing a pydantic model to `headgate.pydantic.writer`. `headgate.pydantic` raises
  `TypeError` naming the class for one it has no schema for, saying decode or encode for a reader or a writer, where it
  raised pydantic's `PydanticSchemaGenerationError`: catch `TypeError`. A codec of your own whose `decoder()` builds
  for a plain class now fails `check_codec`: raise `TypeError` there.
- `headgate.testing.check_codec(codec, record=...)` holds the record type to the strictness it holds the built-in
  containers to: a record whose `int` field holds a numeric string (`{"value": "1"}`), and an array where the record is
  declared, must be refused with one of the codec's `errors`. A codec that built its records by calling the class
  passed, reading `"1"` as text in an `int` field, and so did one that raised an `AttributeError` for an array, which
  a call raised outside the `ClientError` tree. Refuse both in the codec's decoder.
- A reader's `refuse=` that returns anything but a `RefusedError` or `None` raises `TypeError` naming `refuse=`, where
  a call raised `ResponseParseError` ("unreadable 200 body") for the caller's own bug; a `response_check=` that does
  the same raises `TypeError` naming `response_check=`, where it raised an `AttributeError`. `RefusedError(kind=)`
  takes an `UpstreamStatusError` subclass, checked when it is built, where `kind=ValueError` was accepted and failed
  when the refusal was raised: write `kind=UpstreamStatusError` or one of its subclasses.
- A call that follows redirects raises `ValueError` before anything is sent when its `Expect` declares a redirect it
  would follow (a 301, 302, 303, 307 or 308), naming the status: the client followed the redirect and handed the
  target's body to the success reader, so the declared answer never ran. A redirect that a one-shot body keeps from
  being followed may still be declared. Pass `follow_redirects=False` to read the redirect:
  `client.get(url, expect=Expect(ok).on(302, location), follow_redirects=False)`. A walk whose pager's reader declares
  one raises from `paginate()`.
- `cache_ttl=` is an option of the reads alone: `get()`, `head()` and `paginate()` take it (`CallOptions`), and a verb
  with a body does not (`RequestOptions` loses it), since a cache stores only GETs and HEADs. `post(..., cache_ttl=5)`
  and the same on `put`, `patch`, `delete` and `query` are type errors, and raise `TypeError` saying why, where they
  were accepted and ignored: leave `cache_ttl=` out there. A wrapper that forwarded `**options: Unpack[CallOptions]`
  to a verb with a body types its options `Unpack[RequestOptions]`.
- `cache_ttl=` on a read that is not cached raises `ValueError` at the call, where it was silently ignored: on a client
  without a cache, under a route with `cache=None`, for a method or path the cache's rules leave out, and on a walk's
  page with a body, a POST. Leave `cache_ttl=` out there, give the client `CallPolicy(cache=HttpCache(...))`, or name
  the path in `HttpCache(paths=)`.
- The repeated-body loop guard runs for any pager that declares `counts_pages = True` (a class attribute: its next
  request is computed by counting, as an offset or a page number, not read from the page), where only `Offset`,
  `PageNumber` and `BodyOffset` walks were checked, by their class. Those three declare it; a counting pager of your
  own, which was walked until `max_pages=` or forever when its server ignored the paging, opts in with
  `counts_pages: ClassVar[bool] = True`. A pager that reads its next request (a cursor, a link) is unchanged: an
  identical body gives it an identical next request, which the repeated-request check refuses.
- `with fake_network(fake) as net:` binds a `FakeNetwork`, where it bound the transport given: write `net.transport`
  for the old `net`, or keep the `FakeTransport` in a name of its own, as `with fake_network(fake):` already does.
- Inside `fake_network`, a client's SSRF guard resolves names through the fake network, in place of the system
  resolver and of a `SecurityOptions(resolver=)` of the SDK's own (test that resolver directly): every name resolves to
  one public address, and `localhost` and the names under it to loopback, which the guard refuses as in production,
  unless `net.resolve` says otherwise. A guarded SDK's test made real DNS queries, and failed offline or on a host
  that does not exist; it now makes none. The guard's checks still run, so a private IP literal is still refused.
- A walk's `payload=`, when its pager has no `write`, is encoded as JSON in `paginate()`, which raises `TypeError`
  there for one that is not builtins, in a walk's words: it raised at the first iteration, and advised `body=`, which
  a walk does not take. Write `payload=to_builtins(query)`, or give the pager `write=writer(Query)`. A pager of the
  protocol's shape with a `write` of its own is told that its write is not used, since it is not a `WritingPager`:
  subclass `headgate.pagination.WritingPager` for it to encode each page's body.
- `Client(observers=)` and `Client(middleware=)` refuse an entry given as its class rather than an instance, with a
  `TypeError` naming it: `observers=[Metrics]` and `middleware=[Signer]` were accepted, and their methods called
  unbound. Write `observers=[Metrics()]` and `middleware=[Signer()]`.
- A walk reads each page's items once, with its pager's `page_items`, and gives a built-in strategy those items for
  its next request, so a strategy's `items=` hook runs once per page, where it ran twice: once to find the next
  request and once for the items yielded. A subclass of a strategy that only adds to it is walked the same way; one
  that overrides `next_page` or `page_items` is walked by its own `next_page`, which reads the page itself, as a
  pager of your own is. A pager of your own has its `page_items` called once for every page, `pages()` iterated alone
  included, and a writing pager's `write` runs once for each page it posts: the bytes it wrote are both what the walk
  remembers the page by and what is sent. Nothing to write differently.
- `headgate.pydantic` raises `TypeError` where the msgspec flavor raises it, so catch `TypeError`:
  - `to_builtins` for a value holding a class pydantic cannot convert (a plain class, a msgspec struct), naming the
    class, with pydantic's error chained, where it raised pydantic's `PydanticSerializationError`, a `ValueError`. A
    model whose own serializer fails still raises `PydanticSerializationError`.
  - `decode` for content that is neither `bytes` nor a `str`, naming `content=`, where it raised `DecodeError` (see
    below).
- The rest of the public surface refuses a value of the wrong kind where it is written, as the client's own options
  do: a `TypeError` naming the option, what it takes and what it got (a `ValueError` for the right kind with a wrong
  value), where each of these was accepted and failed later, or failed with an `AttributeError`, a `KeyError` or a
  library's own message. Pass each parameter the kind it takes; nothing changes for code that already does. Newly
  refused:
  - `headgate`: `EncodedBody(content=)` that is not `bytes` (a `str` included: encode it) and `content_type=` that
    is not a `str`; `Expect(accept=)` that is not a `str` (an `AttributeError`); `HttpCache(storage=)` that is not a
    hishel `AsyncBaseStorage`, and `partition=` that is not a `Partition`, a plain string included (see
    **Security**); `Multipart(files=)` that is neither a mapping nor a sequence of `(name, file)` pairs, or holds a
    field name that is not a `str` or a file of none of an `UploadFile`'s shapes, naming its field (a number, a list,
    a file opened in text mode, a tuple of one item or of five, a filename or content type that is not a `str`, or
    headers that are not a mapping of `str` to `str`), each of which failed inside httpx2 at the send (a file object
    is anything with a `read`, as httpx2 reads one, and one with only its `read` is sent once, as a file that cannot
    be rewound);
    `Paginated(pages, items_of)` that cannot be called; `Reader(name=)` that is not a `str` or `None`;
    `RetryPolicy(backoff=)` that is not a `Backoff`, `respect_should_retry=` that is not a `bool`, and `hedge=` that
    is not a `Hedge` or `None`; `body_reader(decode)` and `refuse=` that cannot be called, and `empty=` that is not
    `bytes` or `None`; `reader_for(allow_empty_body=)` that is not a `bool` and `refuse=` that cannot be called;
    `decode_with`, `reader_for` and `writer_for(codec=)` that is not a `Codec` (an `AttributeError`), or is a
    codec's class, which has a codec's members as functions (pass an instance, `MsgspecCodec()` or
    `headgate.msgspec.CODEC`);
    `Timeouts.over(base=)` that is not a `Timeouts`; `CachedTokenAuth.seed(minted)` that is not a `Minted`, the token
    alone included (an `AttributeError`: write `seed(Minted(token, lifetime))`);
    `CachedTokenAuth.reject_credential_rotation(returned=, configured=)` given a credential that is not a `str`,
    naming both kinds (`bytes` against a `str` read as a rotated credential, a `ClientConfigurationError`: decode it
    first); and `Paginated.collect(max_items=)` that is not an `int`, a `bool` included, which raised `ValueError`.
  - `decode_with(content=)`, and each flavor's `decode(content=)`, take `bytes` or a `str` (such as a server-sent
    event's data, read as its UTF-8 bytes, so both flavors and any codec read it alike) and refuse anything else with
    the same `TypeError`, naming both: a `bytearray`, which both flavors read, and a `memoryview`, which msgspec reads,
    included. Write `bytes(buffer)`. Any other value raised msgspec's "a bytes-like object is required", or the
    pydantic flavor's `DecodeError`. A `str` holding a lone surrogate, which no UTF-8 holds, raises `DecodeError`.
  - `headgate.msgspec` and `headgate.pydantic`: `MsgspecCodec`, `PydanticCodec`, `decode`, `reader`, `writer` and
    `to_builtins` refuse a `strict=`, `drop_none=` or `allow_empty_body=` that is not a `bool`: `strict="false"`,
    which is truthy, decoded strictly.
  - `headgate.pagination`: `PageRequest(url=)` that is not a `str`, a t-string or an `httpx2.URL`, and `params=`
    that is not a mapping, as a call's `params=` refuses one: a list of pairs, which it read into a dict, included
    (write the dict), where anything else raised "object is not iterable"; `PageRequest.with_param(name=)` that is
    not a `str`; `Offset(size_param=)` that is not a `str` or `None`.
  - `headgate.cache`: `Validator(etag=, last_modified=)` that is not a `str` or `None`; `Validator.of(response=)`
    that is not an `httpx2.Response` (an `AttributeError`), and `held=` that is neither a `Validator` nor `None` (an
    `AttributeError` for a response missing a part, and passed over for one carrying both); `Fresh(validator=)`,
    `NotModified(validator=)` and `conditional(validator=)` that is not a `Validator` (an `AttributeError` from
    `conditional`).
  - `headgate.streaming`: `Stream.aiter_bytes(chunk_size=)`, `aiter_lines(max_line_size=)` and
    `aiter_events(max_event_size=)` that is neither an `int` nor `None`, at the call, `aiter_events` before it checks
    the content type, and one below 1, a `ValueError`: `chunk_size=0` yielded empty chunks in place of the body and a
    negative one dropped part of it, and a `max_line_size` or `max_event_size` below 1 refused every line or event as
    too large. Pass `None` for chunks as they arrive, or for no limit.
  - `headgate.health`: `breaker_snapshots(registry=)` that is not a `BreakerRegistry` (a `KeyError`).
  - `headgate.timing`: `Timing(clock=, sleep=, wall_clock=, random=)` that cannot be called;
    `timing_override(timing=)` that is not a `Timing`, when its block is entered.
  - `headgate.curl`: `AsyncCurlTransport(impersonate=)` that is not a `str`, `default_headers=` that is not a
    `bool`, `curl_options=` that is not a mapping (an `AttributeError`) or has a key that is not a `CurlOpt`, and
    `max_connections=` that is not an `int` (a `bool` and a `float` included), or is below 1, a `ValueError`:
    curl_cffi never finished building its session on 0. An option's name as a string failed at the first request,
    inside curl_cffi, and its number was taken as it is; write the member, `CurlOpt.FRESH_CONNECT`, or `CurlOpt(74)`
    for a number.
  - `headgate.otel`: `OpenTelemetryObserver(tracer_provider=)` that is not a `TracerProvider`, `meter_provider=` that
    is not a `MeterProvider` (each an `AttributeError`), and `propagator=` that is not a `TextMapPropagator`, or
    `None` for any of them.
  - `headgate.testing`: `CassetteFilter(auths=)` that is not iterable and `replaying=` that is not a `bool`;
    `ConnectFail(timeout=)` that is not a `bool`; `Respond(content=)` that is not `bytes`, and `json=` that is not
    builtins JSON can carry, refused where the `Respond` is built, as a `Json` body's value is (a value `json.dumps`
    cannot write failed at the send, and `NaN` or an infinity went out as `NaN`, which is no JSON: a test that wants
    a body that is not JSON writes it as `content=`); `FaultyTransport(schedule=)` that is not an iterable of steps,
    naming an entry that is not one;
    `fake_network(transport=)` that is not an `httpx2.AsyncBaseTransport`, when its block is entered;
    `check_codec(codec=)` that is not a `Codec` (an `AttributeError`), or is a codec's class, as `reader_for`
    refuses one; `FakeTransport.reply(method=, path=)` and `calls_to(method=, path=)` that is not a `str` (an
    `AttributeError` for the method, and a path that matched nothing), and a reply that is none of a reply's kinds;
    and `TestClock.advance(seconds=)` and `sleep(seconds)` that is not a number. A status outside 100-599, which no
    response has, raises `ValueError` where it is written, as a bare status in `FakeTransport.reply()` and as
    `Respond(status_code=)`, both of which the fakes answered with: to test a server that sends one, reply with an
    `httpx2.Response` of it.
- One header spelled two ways in one object raises `ValueError`, where one spelling won unseen:
  `HttpOptions(user_agent=)` beside a `User-Agent` in its `headers=` (the header won, and the product token was
  dropped), and `Expect(accept=)` beside an `Accept` in its `headers=` (`accept=` won). Give `user_agent=` for a product
  token sent before headgate's, or the header for the whole of it; give `accept=`, which each response is checked
  against too, or the header alone.
- Three refusals a first call meets say what to write: `expect=` given a class or a type names its reader
  (`expect=Task`: write `reader(Task)` with a flavor's reader; `expect=str`: `as_text`); `body=` given a value that
  is no body names the body that holds it: `body=Raw(value)` for bytes or an async stream, `body=Raw(value.read())`
  for a file, `body=Json(value)` for a mapping, and for a model's instance (a dataclass's, a msgspec `Struct`'s, a
  pydantic model's, an attrs class's) its writer, `body=writer(Task)(value)`, or `body=Json(to_builtins(value))`;
  and a codec's reader refusing an empty or blank body says it is empty and that `allow_empty_body=True` reads one as
  an empty list, where it gave the parser's words (`Input data was truncated`). The errors' types are unchanged.
- `FakeTransport.reply()` and `calls_to()` refuse a route that can match nothing with a `ValueError` saying what to
  write: one with a query or a fragment, which no route matches (`"/v2/tasks?page=2"`: write `"/v2/tasks"`), a
  relative path (`"v2/tasks"`: start it with `/` and the client's base path), and an absolute URL that is malformed
  (`"http://[::1"`, which raised httpx2's `InvalidURL`) or neither `http` nor `https` (`"ftp://..."`). It was
  accepted, and every request to it failed as `UnmatchedRequestError`. A route path holding a character a URL
  escapes, a space or a non-ASCII one, is escaped as an absolute URL's path is, so `"/v1/a b"` matches the
  `/v1/a%20b` a t-string sends, as `"https://api.example/v1/a b"` does, where it matched nothing. A class as a reply
  (`reply("GET", "/x", Respond)`) raises `TypeError`, where its route failed at the first request: reply with an
  instance, `Respond(200)`.
- A set of default headers names each header once: `HttpOptions(headers=)`, a view's `with_options(headers=)`,
  `Expect(headers=)` and `StaticHeaderAuth(headers=)` raise `ValueError` naming a header named twice, by two lines of
  an `httpx2.Headers` or by keys that differ only in case, where it is given. Defaults are merged by name, and such a
  header was sent in a form nobody wrote. Two lines went out as one, their values joined with `", "`: a server read
  `Cookie: a=1, b=2` as the one cookie `a` with the value `1, b=2`, and two `If-Modified-Since` dates made a header no
  server can read. Keys that differ in case went out joined from a view, as two lines from `HttpOptions`, and as one
  of them alone from an `Expect` (the last) or a `StaticHeaderAuth` (the first). Give the header once, its values
  joined in one string: `{"Cookie": "a=1; b=2"}` for a cookie's pairs, `{"Accept": "application/json, text/csv"}` for
  a list header's values. A call's own `headers=` still sends each line as given, as httpx2 does.
- `headgate.Codec` is runtime-checkable: `isinstance(value, Codec)` asks whether a value has the protocol's five
  members, as `reader_for`, `writer_for`, `decode_with` and `headgate.testing.check_codec` now do, raising
  `TypeError` naming `codec=` for a value that lacks any, where they raised an `AttributeError` naming the member.
  `issubclass` stays unsupported, as for any protocol with members that are not methods.
- `headgate.testing.FaultCall` is frozen, as every other exported dataclass is: `FaultyTransport.calls` returns each
  send as it stands when read, where it returned the records the transport went on filling in. A `FaultCall` read
  before its response was closed keeps `response_closed=False`, so read `calls` again afterwards, as `fake.calls` is
  read; setting a field raises `dataclasses.FrozenInstanceError`. Each holds a copy of the headers sent, so editing
  one changes neither the log nor another read of it.
- A body is given to `headgate.testing.Respond` one way: `Respond(json=..., content=...)`, where `json=` won and
  `content=` went unsent, raises `ValueError`; give one. A `json=` is checked where the step is built, as a `Json`
  body's value is, and sent as before (`json.dumps`'s spelling, a space after each separator and the text escaped to
  ASCII).
- Refusals keep one rule for their type, and name what was refused as the call reads: a `TypeError` for a value of
  the wrong kind, a `ValueError` for the right kind with a wrong value. `paginate(on_limit=)` given a value that is
  not a `str` raises `TypeError`, where it raised `ValueError`; a `str` other than `"raise"` and `"stop"` is still a
  `ValueError`. `Expect.on()`'s refusal of a status outside 100-599 names the option (`status= takes an HTTP status,
  100 to 599 (got 999)`, where it read `999 is not an HTTP status`), and so does `TestClock.advance()`'s and
  `sleep()`'s of a negative, NaN or infinite step (`seconds=`, and `sleep(seconds)`, whose parameter is
  positional-only). A positional-only parameter is named as its call reads, `Route(fragment)`, where the refusal named
  `fragment=`, a keyword `Route` does not take. Code that matched a refusal's text matches the new one.
- `HttpCache(ttl=)` is required, so every cache states its lifetime: `HttpCache(storage=...)` alone kept every 200 GET
  with no expiry, past a `no-store` and never revalidated, and now raises `TypeError` (missing `ttl`). Write
  `ttl=None` to keep entries with no expiry, as before, or the seconds an entry may be served.

### Removed

- The cache's `ClientConfigurationError` for an auth that does not declare its credentials, which no auth can now
  reach. `Partition.SHARED` remains the opt-in for public data.
- `CachedTokenAuth.mint_token()`, which made the exchange on an `httpx2.AsyncClient` of its own: outside the client's
  stack, so its rate budget, retries, SSRF guard, redirect hardening and deadline did not reach it, and outside
  `fake_network`, so a test sent its client secret to the real token host. Yield the exchange's request from `mint`
  instead (see the auth guide), or, for a mint that is not an HTTP request, yield only the `Minted`. The guide's recipe
  for it, and the testing guide's note that it does not reach the fake, are gone with it.
- `BreakerPolicy.registry`, `BreakerRegistry.bind()` and the `ValueError` for a client whose thresholds differed from
  the ones a shared registry was bound to: a registry carries its own policy, so its clients cannot disagree. Give the
  registry the thresholds, and the clients the registry.
- `RetryBudget.record_first()`, `try_spend()` and `refund()`, which only the retry layer calls, and
  `RetryPolicy.wait_after()` and `Backoff.delay()`, the arithmetic it waits by: a budget's surface is its `share` and
  its `repr`, which shows what its window holds (`RetryBudget(RetryShare(...), retries=3, firsts=40)`), and a policy
  and a backoff are values, read by their fields. A test that spent a budget by hand sends its retries through a
  client instead; one that computed a wait reads `factor` and `max_wait`.
- `CircuitBreaker` from `headgate.breaker`, and `BreakerRegistry.breaker_for()`, `breakers()` and iteration, which
  handed out the live breakers only the client drives: `headgate.health.breaker_snapshots(registry)` is the one reader
  of a registry's breakers, building none. Write `breaker_snapshots(registry).get("https://api.example")` for
  `registry.breaker_for("https://api.example")`, reading `.state`, `.refusing` and `.failures` (for
  `.failure_count`) from the snapshot, and `for origin, snapshot in breaker_snapshots(registry).items()` for
  `for breaker in registry`. Each key is the canonical origin, `scheme://host[:port]` with the host lower-cased and
  the scheme's default port left out, and an origin no client has called yet is absent (`.get()` returns `None`),
  where `breaker_for()` built a closed breaker for it. A registry keeps its constructor, `policy`,
  `max_hosts`, `len()`, `clear()` and its `repr`, which shows its policy and `max_hosts` beside each origin's state:
  `BreakerRegistry(BreakerPolicy(...), max_hosts=1024, breakers={'https://api.example': 'open'})`.
- `headgate.health.cache_degradations()` and the process-wide count behind it, which repeated what the
  `CacheChecked` event already reported, without the call: count the `CacheChecked` events whose outcome is
  `CacheOutcome.DEGRADED` (a lookup found the storage broken) or `CacheOutcome.UNSTORED` (an accepted answer could not
  be stored) in an observer, per `httpx2.URL(event.call.full_url).host` for the old per-host shape. The count held
  both. `observer_failures()` stays, since an observer that raises cannot be reported through observers.
- `AddressGuard`, `PinnedHTTPTransport` and `resolve_with_loop` from `headgate.security`, and
  `AsyncCurlTransport.bind_guard()`, the SSRF guard's machinery, which a client builds and binds from its
  `SecurityOptions`: configure the guard there. Write
  `HttpOptions(security=SecurityOptions(block_private_hosts=True, check=...))` where a test built
  `PinnedHTTPTransport(AddressGuard(check=...), ...)` to reach a loopback server, and pass an `AsyncCurlTransport` as
  `HttpOptions(transport=)` where it called `bind_guard()` itself.
- `HttpCache.caches_request()` and `stores_status()`, `headgate.pagination.parse_link_header()` and `Link`, and
  `headgate.testing.Respond.response()`, which only the stack and the fakes call. Read what a cache stores from its
  `methods`, `paths` and `statuses`, follow `Link` headers with `LinkHeader`, and read a `Respond` step's
  `status_code`, `headers`, `json`, `content` and `retry_after`. A subclass of `HttpCache` that overrode either rule
  raises `TypeError` when it is defined (see **Changed**): give the cache the `methods`, `paths` and `statuses` it
  should keep.

### Fixed

- Under `instant_waits()` (`instant_retries()` in 0.12.1), a call that waits out a server's pause (a 429's or 503's
  `Retry-After`, or a spent quota) sleeps through it once and sends. The sleep returned at once, but the wait read a
  clock the sleep had not moved, saw the pause still running and slept again: it spun until the real clock passed the
  pause, with a `RateLimited` event each time round (some 200,000 for a 3-second pause), and inside
  `TestClock.install()` as well, whose clock nothing moved, it never ended. Nothing to write differently.
- A failure the breaker counts is charged though the retry after it never went out: when the rate budget withdrew
  the retry (its turn came after the call's deadline), the call ended on the first attempt's 5xx, or network failure,
  and charged the origin nothing. Nothing to write differently.
- `copy.copy()` and `copy.deepcopy()` of a `RateBudget`, a `RetryBudget` or a `BreakerRegistry` return the object
  itself, so a deep copy of a `CallPolicy`, or of anything else holding one, keeps sharing it. A copy was a new object
  without the pacer, ledger or breakers the stack finds it by, which failed later with a bare `KeyError`, and a deep
  copy failed with `cannot pickle '_thread.lock' object`. Nothing to write differently. Pickling one, or anything
  holding one, raises `TypeError` saying it is live state its clients share within one process, where it raised
  `cannot pickle '_thread.lock' object`: build one in each process that uses it.
- The value of every exported type alias evaluates at runtime: `headgate.Next.__value__`,
  `headgate.security.Resolver.__value__` and `headgate.security.AddressCheck.__value__` raised `NameError` for
  `Callable`, which their modules imported only for the type checker, so a library that reads an alias's value
  (pydantic, `typing.get_type_hints` on a signature that names one) failed. Nothing to write differently.
- A breaker's consecutive-failure count includes a failed half-open probe, so `BreakerSnapshot.failures` and
  `BreakerChanged.failures` say what they are documented to: a breaker with `failure_threshold=2` whose probe failed
  reads `failures=3`, where it read `2` and kept reading it until the next success. Nothing to write differently; a
  test that pinned the old count after a failed probe adds one.
- A `CachedTokenAuth` whose token type is not `str` no longer sends the token's `repr` as its bearer
  (`Authorization: Bearer {'access': ..., 'refresh': ...}`, the refresh token included). Its class must override
  `attach`: a subclass of `CachedTokenAuth[dict[str, str]]` that keeps the default raises `TypeError` when it is
  defined, and the default `attach` raises `TypeError` for a token that is not a `str`, before anything is sent, where
  the type is bound further down, or may be text (`CachedTokenAuth[Any]`, `CachedTokenAuth[str | None]`). Override
  `attach` to put the token on the request.
- A `CachedTokenAuth` that keeps the default `attach`, which sets `Authorization`, must name `authorization`
  (in any case) in `credential_headers`: a narrowed declaration (`credential_headers = frozenset({"x-other"})`) raises
  `TypeError` when the class is defined. It sent the bearer undeclared, and its 401s never re-minted, since a 401
  re-mints only for a request that carried a declared name. Keep `authorization` in the declaration, or override
  `attach` to put the token where the declaration says.
- `CallInfo.full_url` is filled for every call, so a middleware reads the absolute URL of a client with no observers
  too, where it read `None`. It is a required `str`: a `CallInfo` built by hand, in a test of an observer, passes
  `full_url=`, such as `CallInfo(method="GET", url="items/1", full_url="https://api.example/items/1")`, and an
  observer drops its `if info.full_url is not None` check.
- `typing.get_type_hints()` resolves on every exported class and function, and on the methods they define, after a
  bare `import headgate`, so `inspect.signature(Client)` reads every option, and `msgspec.convert({"read": 5.0},
  Timeouts)` and pydantic build the value options (`Timeouts`, `Rate`, `Backoff`, `RetryShare`, `BreakerPolicy`) from
  configuration data; `RetryPolicy` and `CallPolicy`, whose fields may hold a shared live object, are built in code.
  They raised `NameError` for a name the annotation imported only for the type checker (`Mapping`, `ClassVar`,
  `httpx2`). headgate's modules no longer use `from __future__ import annotations`; their annotations are
  Python 3.14's own, evaluated on demand in the module that wrote them. Nothing to write differently.
- The codec guide's cattrs-over-orjson codec keeps the strictness headgate's codecs keep, and passes `check_codec`:
  copied as it was written, it read `"1"` as `1` where an `int` is declared, and built a decoder for a type it could
  not structure. It registers hooks that take a scalar only as itself, and raises `TypeError` from `decoder()` for a
  type cattrs has no hook for: copy the new version. The `Codec` protocol says that a codec handles the built-in
  containers, which `allow_empty_body=True` and `check_codec` rely on.
- The guides' samples are type-checked with every rule an error, and those that need no server are run, by the test
  suite, so a spelling the API no longer has cannot survive in them. The samples it found wrong are fixed: an
  `Auth` without `@override`, a conditional read typed on the SDK rather than its `Client`, a deadline sum over
  optional phases, a pager example whose page type clashed with the page's other one, and missing imports.
- The guides and the README say what the code does. A `CachedTokenAuth` does not buffer a streamed body for its 401
  re-send, a `SigningAuth` does; `head()` requires `expect=`, as every verb does; only a t-string is escaped, and a
  computed `str` is sent as written, refused only by a checker that enforces `LiteralString` (ty, pyright); a lambda
  given a pager is checked for its use of the page, not for what it returns; a middleware is a class, since a function
  with the declarations set on it does not type-check; `max_line_size` counts characters, where `max_event_size` counts
  bytes; a cached entry's lifetime runs on the system clock, not a `TestClock`; `ClientError` is the base of every
  failure, not of a transport's alone; time is set through `timing_override()`, not a `timing=` on each constructor.
  They say what they left out too: a token mint's hops run under the client's retry policy, not the call's (a POST mint
  is re-sent only with an idempotency key), and through its middleware, each a call of its own to `call_view()`, with no
  event; a middleware's own 5xx is retried as the network's is; an unpaced client's `budget_for()` is a budget that
  waits out a server's pauses and paces nothing. Every README sample imports what it uses, every guide sample sorts its
  imports as ruff does, which the doc-sample test checks with ruff's import rule, and the testing guide sets up
  pytest-asyncio and has its fixture yield the fake beside the SDK, which it closes.
- `help()` of the public API names only what a user can import: no docstring cites a private module
  (`headgate._url.render_path`, `headgate._failures.mint_error`, `headgate.security._logging`) or a private name
  (`hold_timing`, `SYSTEM_TIMING`, `AUTH_HOP`, `FRESH_TOKEN_S`); each is cited by its public home, or its value is
  stated (a token minted in the last 60 seconds is not re-minted on a 401; a walk remembers 4096 pages). A test keeps
  it so.
- `Client(middleware=)` and `Client(observers=)` name a `functools.partial` they refuse by the function it wraps,
  `middleware partial(stamp) must declare ...`, where they named every partial `partial`. Nothing to write
  differently.
- An entry of the wrong kind in a collection option raises `TypeError` naming the option and the entry, such as
  `paths= takes a collection of path fragments or patterns, such as ('/items',) (got int 1 among them)`.
  `HttpCache(paths=[1])` raised a bare `AttributeError`, a pattern compiled from bytes `re`'s own `TypeError`,
  `SecurityOptions(trusted_origins=[5])` httpx2's `Invalid type for url`, and a collection of names holding what is
  not a `str` said it got a `list` (`HttpOptions(request_id_headers=)`), or failed on the entry (`methods=["GET",
  None]` an `AttributeError`, `CassetteFilter(headers=["x-key", b"x-other"])` a comparison error). Nothing to write
  differently.
- `headgate.msgspec.decode()` and `decode_with()` raise `DecodeError` for a JSON string whose bytes are not UTF-8,
  `not UTF-8 text: a JSON string holds bytes that are not UTF-8`, where msgspec's own `UnicodeDecodeError` escaped;
  inside a call it was already a `ResponseParseError`. It names no byte, since msgspec counts the bad one within the
  string, not the body. Catch `DecodeError`.
- `headgate.testing.Respond(headers=httpx2.Headers([...]))` sends each line of a header given twice, as a response may
  (`Set-Cookie`), where it sent them joined into one line, which a cookie jar reads as one cookie. Nothing to write
  differently.
- `headgate.testing.Respond` sends `json=` under the `Content-Type` its `headers=` set, where it added
  `application/json` beside it, a second line no reader takes: an `application/problem+json` error read no `problem`,
  and `Expect(accept="application/vnd.api+json")` refused the body as `application/vnd.api+json, application/json`.
  Nothing to write differently. A `retry_after=` beside a `Retry-After` in `headers=` raises `ValueError`, where both
  went out: give it one way.
- `params=httpx2.QueryParams([("a", "1"), ("a", "2")])` sends every value, `?a=1&a=2`, on a call and on every page
  of a walk, where it sent only the first, `?a=1`, losing the rest without a word. Nothing to write differently; a
  mapping of lists, `params={"a": ["1", "2"]}`, sends the same.
- `Client(base_url=)` must be an `http` or `https` URL with a host and nothing after its path, and raises `ValueError`
  naming `base_url=` and the fix where the client is built. Only its kind was checked, so a URL each call's path could
  not be joined onto was accepted: `"api.example.com"` raised httpx2's "URL must be absolute to have an origin" (write
  `"https://api.example.com"`); `"ftp://..."` failed at the first call; a query (`"https://api.example/v1?key=..."`)
  had every path appended to it, key and all (send a key with `QueryParamAuth`, other parameters with `params=`); a
  fragment was joined around the path; and a user and password (`"https://user:pw@..."`) went out as an
  `Authorization` header no auth declares (use an `Auth`, such as `StaticHeaderAuth({"Authorization": ...})`). The
  refusal shows the URL redacted and cut after its first `?` or `#` (`'https://api.example/v1?…'`), since a secret may
  sit under any query name.
- A network failure raised before the attempt reached the network no longer opens the origin's breaker: a middleware's
  own `httpx2.ConnectError` (an offline mode), a `SigningAuth.sign` that raises `OSError` (a missing key file), or the
  SSRF guard's failed lookup of the call's own host over a custom transport sent nothing and tested no origin, yet
  each was charged, so a client whose signer had no key opened the breaker of a healthy origin, for every client of a
  shared `BreakerRegistry`. An attempt counts as sent once it is handed to the network transport, below the signer,
  and only such an attempt's failure is charged. Nothing to write differently.
- A `CachedTokenAuth` given to clients on two event loops (one per thread, or one `asyncio.run` after another) mints
  on each: its single mint lock was an `asyncio.Lock`, bound to the first loop that waited on it, so a mint contended
  on a second loop raised a bare `RuntimeError` ("bound to a different event loop"). Each running event loop now has
  its own mints, under a lock of its own made under a `threading.Lock`: the calls of a loop wait for one mint, and a
  call behind a mint that failed raises that mint's error, while the loops mint apart and share the token. Two
  threads that use a new auth at once make one state for it between them. The auth copies, deep-copies and pickles,
  before a call and after: a copy starts with an empty cache of its own, and a pickle holds neither the mints nor the
  token. Nothing to write differently.
- Clients that share one `headgate.curl.AsyncCurlTransport`, as the security guide says clients with the same SSRF
  guard may, keep it open until the last of them closes: the first client's `aclose()` closed the curl session under
  the others, whose calls then failed. Each client built on it holds the transport, and the last to let go closes its
  session; `transport.aclose()` still closes it at once. Any other transport is closed by the client it is given to,
  when that client closes, as before: give each client its own, or close the clients together. A client built on a
  curl transport that is closed (by its last client, or by its own `aclose()`) raises `ValueError` where it is built:
  it was built, and each of its calls failed with `Session is closed`, retried and charged to the origin's breaker.
  Build a new transport for it.
- A subclass of `StaticHeaderAuth`, `BearerAuth`, `QueryParamAuth`, `SigningAuth` or `CachedTokenAuth` that overrides
  `auth_flow` raises `TypeError` when it is defined: each writes its own `async_auth_flow`, the only flow a client
  runs, which never calls `auth_flow`, so the override (a tenant header added before the bearer, say) was accepted and
  never ran. An `Auth` subclass that writes no `async_auth_flow` runs its `auth_flow` through httpx2's, as before, and
  one below an `async_auth_flow` of your own, which may drive `auth_flow`, is yours to override. Set a header computed
  per request in a `SigningAuth`'s `sign()`.
- A key an `IdempotencyKeys(factory=)` returns that is not a `str` raises `TypeError` naming `factory=`, before
  anything is sent: `None` or an `int` failed with a bare `AttributeError` (`'int' object has no attribute 'strip'`),
  and `bytes` were sent as the key. Return the key as text, as the default `str(uuid.uuid4())` does.
- `HttpOptions(proxy=)` refuses a URL that names no host, with `ValueError` where it is built:
  `"http:user:pw@proxy.example:8080"`, missing its `//`, or `"socks5://:1080"` was taken, and each call then failed
  with `TransportError` ("All connection attempts failed"). Write `"http://user:pw@proxy.example:8080"`.

### Security

- A `SigningAuth` whose `sign` changes the request's origin is refused before anything is sent, with a `TypeError`
  naming both origins: the request had been vetted for its origin, and went to the new host, signature and declared
  credentials with it, over a custom transport, which the SSRF guard does not pin. Change a URL in a middleware,
  which runs before the checks.
- `HttpCache(partition=)` takes a `Partition` and nothing else. A plain string was accepted, and the cache compares
  the member itself, so `partition="credential"`, as a configuration file spells it, kept no entry apart by
  credential: a client was served the entry another credential had fetched on the same storage. A string now raises
  `TypeError` where the cache is built: write `Partition.CREDENTIAL`, or `Partition(value)` for a value read from
  configuration.
- An auth's own header or query parameter no longer follows a request to another origin. A plain `httpx2.Auth` that set
  `X-Api-Key` sent it on a cross-origin redirect and to an absolute URL on another host, since the guard withheld only
  `Authorization`, `Proxy-Authorization` and `Cookie` for an auth that declared nothing; such an auth is now refused.
- A credential declaration whose names are all one character long is refused with a `TypeError` that shows the spelling
  to write: `frozenset("X-Api-Key")` is a `frozenset[str]` of eight letters, so it type-checked and withheld no real
  header. Write `frozenset({"X-Api-Key"})` (`frozenset({"api_key"})` for a parameter). A lone one-character name
  (`headers=["x"]`) is refused too, since no header or query credential is one character long. It is checked for a
  middleware when the client is built, for an `Auth` subclass's class attribute (`credential_headers` and
  `credential_params`, the one spelling `SigningAuth` and `CachedTokenAuth` declare with too) when the class is defined,
  for a plain `Auth`'s declaration through a property when the client is built (a property on a `SigningAuth` or
  `CachedTokenAuth` is refused when the class is defined), and for `CassetteFilter(headers=, params=)` and its auths'
  declarations when the filter is built.
- The cache never looks up, stores or shares a call given a body, whatever its method and whatever `Content-Length` the
  caller wrote: a GET stream opened with `body=` was served the entry stored for its URL. No GET or HEAD takes a body
  now; this is the cache's own defense.
- `CassetteFilter(headers="x-api-key")` no longer records the header it was asked to scrub: it filtered the letters
  `x`, `-`, `a` and so on. A bare `str` is now refused.
- Every pager's next-page `httpx2.URL` is resolved against the URL that answered the page, and a next page from
  `https` to plain `http` is refused as `ForbiddenDestinationError` before it is sent, by the walk itself. Only the
  built-in `LinkHeader` and `NextUrl` did this, so a custom pager could take a walk, its cursor and its request, to
  cleartext, and its relative URL resolved against `base_url`. A pager returns the URL it read as
  `PageRequest(httpx2.URL(target))`, as before, and need not resolve it. An absolute URL a pager forwards as a `str`
  (read from a JSON body, say) is checked the same way; a relative `str` is still joined onto `base_url`, as a call's
  own URL is.
- An auth whose class escaped the class-definition checks is refused by `Client()` with a `TypeError`. A base class
  whose `__init_subclass__` did not call `super().__init_subclass__()` let every subclass below it skip them all: an
  undeclared or misdeclared credential, an `async def sign`, an overridden flow. Call
  `super().__init_subclass__(**kwargs)` in every `__init_subclass__` of an auth class.
- What an auth's hook stamps must be what it declares. A `SigningAuth.sign` or a `CachedTokenAuth.attach` that sets a
  header or query parameter it does not declare raises `TypeError` naming it, before the request is sent: the guard
  withholds only declared names, so a signer that also set `X-Api-Key` sent that long-lived key along a cross-origin
  redirect. Add every name the hook sets to `credential_headers` or `credential_params` (headers compared
  case-insensitively); a name the request already carried is not counted.
- A query parameter a middleware declares in `credential_params`, and one an auth of your own declares there, is
  redacted from every URL headgate shows, httpx2's own `HTTP Request:` log line included: the client registers the
  names its auth and middleware declare when it is built. Only `QueryParamAuth`, `SigningAuth` and `CachedTokenAuth`
  registered theirs, so a middleware that stamped `?tenant_key=...` had the key printed in clear at INFO. Nothing to
  write differently.
- A header the client's auth sets cannot be a default: `HttpOptions(headers=)` or `with_options(headers=)` naming
  one the auth declares in `credential_headers` (compared case-insensitively) raises `ValueError` naming the header
  and the auth's class, where the client or the view is built. Every request carried it before the auth ran, and an
  auth takes a request already carrying one of its headers as the caller's own credential: a `SigningAuth` whose key
  id was also set client-wide signed nothing and re-sent no 401, a `CachedTokenAuth` minted nothing, and a
  `BearerAuth` sent the default's value instead of its token. Leave the header to the auth; a call that authenticates
  itself still sets it on its own `headers=`.
- The SSRF guard vets the host a middleware sends to over a network transport that does not pin, a custom
  `HttpOptions(transport=...)` or a test's `fake_network`: a middleware that changed `request.url`'s host (a failover
  to a replica, say) reached the transport with a private host unvetted, since the guard's check over such a
  transport ran only as a request hook, before the middleware. It now runs again below the last middleware on any
  attempt whose origin the guard has not admitted (an attempt left where the hook vetted it is not looked up twice,
  while a retry or a hedge of one a middleware moved is vetted where it goes, unless the guard admitted that origin);
  inside `fake_network`, through the fake network's names, so a test sees the refusal the default transport, which
  pins where it connects, gives in production. Nothing to write differently.
- A client built outside every `fake_network` block (a module-level SDK, a session fixture built before the block)
  refuses each call made inside one with `headgate.testing.UnmatchedRequestError`, naming the call and saying to build
  the SDK inside the block, before anything is sent. It sent on its own network, the real one, with the test's token,
  while the fake saw nothing. The error is an `AssertionError`, not a `ClientError`, so an SDK's own
  `except ClientError` and its retries do not swallow it. A client built on the block's own transport
  (`HttpOptions(transport=fake)`) sends as before, and so does one built in another block, on that block's fake, and
  one built on a `FakeTransport` or `FaultyTransport` of its own, which reaches no network, on that fake. The block is
  the calling task's context: a task started before it was entered, and another thread, run outside it, so their
  calls are neither refused nor faked. Build the SDK inside the block, typically in a fixture.
- `fake_network(transport)` takes a fake, which answers in place of the network: a transport that sends on the real
  network, `httpx2.AsyncHTTPTransport` or `headgate.curl.AsyncCurlTransport`, raises `TypeError` where the block is
  entered. `fake_network(AsyncCurlTransport(...))` switched a guarded client's SSRF guard off: the curl transport vets
  and pins each request itself once a client's guard is bound to it, which a block never did, so every hop reached
  its host unvetted, a loopback or private one included. Every client built in a block now vets each hop as a request
  hook, whatever transport the block was given. Give the block a `FakeTransport`, and build a client that must reach
  a real server outside it.
- The SSRF guard refuses a `headgate.curl.AsyncCurlTransport` whose `curl_options` set `CurlOpt.UNIX_SOCKET_PATH`,
  `ABSTRACT_UNIX_SOCKET` or `PORT`, with the `ValueError` it raises for a `PROXY`, `PRE_PROXY`, `RESOLVE` or
  `CONNECT_TO` there, where the client is built: curl connected to that socket in place of the host the guard vetted,
  so a guarded client reached a local service (a Docker or database socket) through a host the guard admitted; and the
  guard pins a host on the URL's port, so on another port curl looked the name up itself. Leave the socket out of a
  guarded client's transport, and put a port in the URL. `CurlOpt.URL` and `CURLU` are refused on any curl transport:
  curl sent every request to that URL, past every hook and the guard. Give the URL as `base_url=` or the call's URL.
- `headgate.curl.AsyncCurlTransport` refuses `curl_options` that put a header or a credential on requests,
  `CurlOpt.HTTPHEADER`, `HTTP3_HTTPHEADER`, `COOKIE`, `USERPWD`, `USERNAME`, `PASSWORD`, `XOAUTH2_BEARER` and a truthy
  `NETRC`, with `ValueError` where it is built. curl added it below every hook, so it went to the other origins a
  redirect led to (a `.netrc`'s `default` entry to any host), past the credential guard that withholds a credential
  there and the redaction of secrets, and a header list added its lines to each request's own, a second
  `Authorization` beside the auth's. `CurlOpt.HEADEROPT` set to `CURLHEADER_UNIFIED` (0) is refused too: curl sent each
  request's headers, an auth's token among them, to the proxy as well, in the `CONNECT` that opens the tunnel. Set a
  header with `HttpOptions(headers=...)` and a credential with an auth (`BearerAuth`, `StaticHeaderAuth`), and leave
  `HEADEROPT` out; a proxy's own credential (`CurlOpt.PROXYUSERPWD`) is taken as before.
- A header no request can carry is refused where it is given: a name that is not an HTTP token (a space, a colon or a
  line break in it), and a value holding CR, LF or NUL, raise `ValueError` naming the option and the header, not the
  value, in `HttpOptions(headers=)`, `with_options(headers=)`, `Expect(headers=)`, `StaticHeaderAuth(headers=)` (and so
  `BearerAuth(token=)`), a call's own `headers=`, `IdempotencyKeys(header=)` (whose empty name raised `header must name
  a header`) and `headgate.testing.Respond(headers=)`. Over the curl transport such a value went out as written, so a
  line break in it (a tenant id taken from an incoming request, say) ended the header there and sent the rest as
  headers the caller never wrote; httpx2's own transport refused it over HTTP/1.1 at the send, and sent it whole over
  HTTP/2, for a proxy that forwards over HTTP/1.1 to split. Check or strip a value taken from outside before it
  becomes a header. A call's `idempotency_key=` and `events(last_event_id=)` are refused alike, and the curl transport
  refuses a header with such a value however it was set (a minted token, a middleware's or a signer's stamp), as
  httpx2's own transport does over HTTP/1.1, with `httpx2.LocalProtocolError`, before anything is sent.
- The `repr` of an `HttpOptions` and of an `Expect` shows each header by its name, with `'***'` for its value
  (`headers={'X-Api-Key': '***'}`), and an `HttpOptions`' proxy URL with its user and password as `***`: a default
  header may carry a key, and a proxy URL a password, which the repr printed in clear, in a log line or a traceback's
  locals. Read a value from `.headers` or `.proxy`, which hold it as given.
- The `ValueError` for a `SecurityOptions(trusted_origins=)` entry that is no origin, and for an `HttpOptions(proxy=)`
  of another scheme, shows no secret: the first quoted the entry whole, a password in its userinfo and a key in its
  query included, and the second named, as the URL's scheme, the user of a URL written without one
  (`token:x@proxy.example:8080`), which may be a token. The entry is shown as `base_url=`'s refusal shows a URL, with
  its userinfo as `***` and nothing after its `?` or `#`, and such a proxy URL is called malformed. Nothing to write
  differently.

## [0.12.1] - 2026-11-16

The SSRF guard now pins the curl transport's connections to the addresses it vetted, as it already pinned the default
transport's, so a name that rebinds after the check is never reached over curl. A redacted query value reads `***`,
and headgate needs httpx2 2.16.1 or newer.

### Added

- `AsyncCurlTransport.bind_guard(guard)` binds a curl transport to one SSRF guard (or to none). A `Client` calls it
  when it is built on the transport, so a caller need not call it.
- `AddressGuard` compares equal, and hashes, by its resolver and check. Two clients whose `SecurityOptions` build equal
  guards may share one curl transport.

### Changed

- A redacted query value now reads `***`, as a redacted userinfo and fragment credential already did, instead of the
  percent-encoded `%2A%2A%2A`: `?api_key=s` shows as `?api_key=***` in error messages, `url` on an error, the
  request event's `full_url`, the OpenTelemetry `url.full`, the redacted httpx2 log line and a recorded cassette's URI.
  A search for `***` now finds every redacted credential. A sanitized URL is for display only and is never sent. A
  cassette recorded before still replays under vcrpy's default `query` matcher, which compares decoded values; one
  matched on the raw `uri` needs re-recording.
- headgate needs httpx2 2.16.1 or newer. A redirect whose relative `Location` joins past httpx2's 65,536-character URL
  limit is now a `TransportError` that counts against the host, as an unparsable `Location` already was, instead of a
  `ClientError`: httpx2 2.16.1 raises `RemoteProtocolError` for both.

### Fixed

- `headgate.health.cache_degradations()` holds at most 1024 hosts, as a `BreakerRegistry` holds at most 1024
  breakers. It gained a key for every host whose cache degraded, so a client calling many hosts grew it without end.
  Past the bound the host least recently degraded is dropped, and counts from zero if it degrades again.
- The curl transport checks a request's host with the SSRF guard before it reads the request body, so a failed
  lookup leaves a one-shot body unsent and the call retryable.
- Closing an `AsyncCurlTransport` a second time does nothing, so clients that share one can each close it. On
  `curl_cffi` 0.16 the second close raised `TypeError`.

### Security

- The SSRF guard now pins `headgate.curl.AsyncCurlTransport`'s connections, as it pins the default transport's. With
  `SecurityOptions(block_private_hosts=True)`, each request's host is vetted and curl is handed the vetted addresses,
  so a DNS answer that changes between the check and curl's own lookup (DNS rebinding) can no longer reach a private
  address. Since curl shares its DNS cache and connections across requests, a curl transport serves one guard:
  building a client on a transport another client already uses with a different guard, or without one, raises
  `ValueError`. The guard also refuses `curl_options` that choose the address (`CurlOpt.PROXY`, `PRE_PROXY`,
  `RESOLVE`, `CONNECT_TO`). Over any transport, the guard's lookup now counts against the connect timeout.

## [0.12.0] - 2026-11-15

The API redesign. A call takes its options and one `body=` in a single place, configuration is grouped by what it
governs, each path's limits are one `Route`, and `Client` is final: an SDK holds one rather than subclassing it. Errors
are read-only, and `retryable` means the request is safe to send again. The top level is 52 names, each exported from
one module. It also fixes every finding of a full review of the code, among them one-shot bodies sent twice below the
retry layer, credentials leaking past redirects and into logs, and the cache answering one caller with another's
response. This minor release changes the public API: each change is listed under **Changed** and **Removed** with what
to write instead.

### Added

- `Client.query(url, *, expect, body=..., ...)` sends the QUERY method, which is safe and idempotent like a GET and
  carries a body like a POST. QUERY joins `IDEMPOTENT_METHODS`, so it is retried after any retryable failure and
  may be hedged. It is never cached. `Method` gains `"QUERY"`, and an OpenTelemetry span for one is named `QUERY`.
- `Client.events(url, ...)` opens a server-sent event stream. It is `stream()` of a GET that sends
  `Accept: text/event-stream` and gives each read no time limit, unless the call's own `headers=` or `timeout=` say
  otherwise. It takes `last_event_id=` and every other option `stream()` takes.
- `headgate.testing.fake_network(transport)`: every `Client` built in its block sends on *transport* in place of the
  network, even one an SDK gave a transport of its own, so an SDK need not forward `transport=` for its tests. Only the
  network is replaced: the SDK's rate budget, cache, retries and breaker run over it.
- `headgate.testing.breakers_of(client)`, the `BreakerRegistry` a client's breakers live in, or `None` for a client
  without a breaker.
- `BreakerRegistry.bind(failure_threshold=, recovery_timeout=)`, which fixes the thresholds of every breaker the
  registry holds, and a `repr` naming each origin's state.
- `TestClock(epoch=..., random=...)` and `TestClock.wall_clock()`: a test clock's wall clock reads `epoch` plus its
  monotonic reading, and `random` is the backoff's jitter draw (`random=lambda: 1.0` for a full backoff a test can
  predict). `RetryBudget.record_first()`, `try_spend()`, `refund()` and `.share`, what a shared budget is spent
  through.
- A `repr` showing their state on `RateBudget` (its rate, the rate pacing runs at now, what is in flight and the pause
  left) and `RetryBudget` (its share, and the retries and first attempts in the window).
- `RetryShare`, the frozen share of traffic retries may add, from which each client builds a budget of its own.
- `headgate.call_info(request)`, a middleware's read-only view of its call: a `headgate.events.CallView` holding the
  call's `CallInfo` (its route template among it), its `attempt` and the seconds `remaining` before its deadline,
  taken when asked, or `None` for a request no client call built. A middleware no longer imports the private
  `CallContext`.
- `headgate.security.CredentialCarrier` and `CredentialStamper`, the protocols an auth or a middleware declares its
  credentials with, public where the cache's error message and the docs now point.
- `headgate.timing`, the one seam for time and randomness: `Timing(clock=, sleep=, wall_clock=, random=)`, its
  protocols `Clock`, `Sleep` and `RandomSource`, and `current_timing()` and `timing_override()`. `Timing` gains a
  `wall_clock`, for `Retry-After` dates and `RateLimit` epochs, and a `random` source, for the backoff's jitter.
- A `Timeouts` phase set to `math.inf` has no limit (`Timeouts(read=math.inf)`), while `None` still takes the layer
  beneath's value. httpx2 is handed `None` for that phase. Under a deadline it gets the time the deadline leaves, and
  it is never refused as longer than the deadline.
- `Offset(size_param=...)` sends the page size under that query parameter (`limit`, `per_page`, SODA's `$limit`).
  It sends the caller's own size there when `params=` give one, and steps by it, and sends `page_size` otherwise. A
  page shorter than that size ends the walk. An endpoint whose callers choose their page size no longer needs a pager
  of its own.
- `ClientError.sent`: whether any byte of the failed call can have reached the network. It is `False` only when no
  byte can have, so the call is safe to send again whatever its method. It is `True` when some may have, so a write
  may have been processed, and `None` on an error no client call raised. It covers every send, retry and followed
  redirect of the call, and never a token mint's own requests.
- `CassetteFilter(replaying=True)`, for replaying a cassette (`record_mode="none"`): a token mint fails the test with
  an `AssertionError` saying to seed the auth. A mint is never recorded, so vcrpy let an unseeded one reach the
  network, with the auth's secret.
- `RouteOptions`, the closed TypedDict of what a `Route` sets: `retry`, `deadline`, `rate` and `cache`.
- `Body`, the union of `EncodedBody`, `Json`, `Form`, `Multipart` and `Raw`, the body classes a call's `body=` takes.
  `Raw(...).replayable` and `Multipart(...).replayable` say whether a retry or a 307/308 redirect can send the body
  again: not for a `Raw` async iterable, nor a `Multipart` holding a file that cannot be rewound.
- `max_response_bytes=` on a call (#57) raises or lowers the client's body cap for that call alone, as `timeout=` and
  `deadline=` already can: higher for one large file on a host whose other files are small, lower for an endpoint that
  should stay small. It is checked as a positive `int`. A cached client holds the body up to the call's cap, so the
  entry is stored, and every read of an entry is checked against the cap of the call reading it. An undeclared error
  status keeps the client's cap, so it still raises as its status. `stream()` takes no cap, since its body is never
  buffered.
- `CallOptions`, the TypedDict of every per-call option but the body, which every verb and `paginate()` take, and
  `headgate.streaming.StreamOptions`, the options of `stream()`. `with_options(follow_redirects=...)` sets a view's
  redirect choice, and is refused on a client with a `response_check`, as each of its calls would be.
- `Stream.retry`: the reconnection time the server last set with an SSE `retry:` field, kept across events as the
  WHATWG spec says, including a `retry:` sent in a block of its own. The `retry` of each event `aiter_events` yields
  (an `httpx2.ServerSentEvent`) is unchanged: the field in that event's own block.
- `Stream.aiter_lines(max_line_size=...)`: a line past it, 1 MiB by default (`None` for no limit), raises
  `ResponseTooLargeError`, before it ends if it never does. A body with no line ending used to be held whole.
- `as_json`, a ready reader of JSON into builtins with the standard library's `json.loads`, so a core-only install
  can read JSON without a codec. It is typed `object`, and reads strict RFC 8259 JSON as `headgate.msgspec`'s reader
  does: a body that is not UTF-8, `NaN`, `Infinity`, a number out of range and a lone surrogate are each a
  `DecodeError` naming the position, never the input.
- `CachedTokenAuth.token_params`, for a provider that takes the token in the query (`?access_token=`): a subclass
  whose `attach` sets a query parameter declares it here, as `token_headers` declares headers.

### Changed

- `DEFAULT_TIMEOUTS` is `Timeouts.DEFAULT`, a class attribute with the same value (connect 10 s, read 30 s, write
  10 s, pool 15 s). It is not a field, so `dataclasses.replace` and `fields(Timeouts)` are unchanged.
- **Each top-level name is exported from the top level only.** `RetryPolicy`, `Rate`, `RateBudget`, `BreakerPolicy`,
  `HttpCache` and `Paginated` are no longer re-exported by `headgate.retry`, `headgate.ratelimit`, `headgate.breaker`,
  `headgate.cache` and `headgate.pagination`: write `from headgate import RetryPolicy`, since
  `from headgate.retry import RetryPolicy` raises `ImportError`. The six top-level errors, `ClientError`,
  `TransportError`, `ClientTimeoutError`, `UpstreamStatusError`, `AuthError` and `RateLimitError`, leave
  `headgate.errors.__all__`: `from headgate.errors import ClientError` still works at run time, since the module
  defines them, but type checkers and the docs now point to `headgate.ClientError`.
- A `params=` value no query parameter can carry raises `TypeError` naming the parameter, before anything is sent,
  rather than going out as its Python `repr` (`filter=%7B%27a%27%3A+1%7D`). The refused shapes are:
  - a mapping (`dict`, or any other `Mapping`);
  - a set or `frozenset` (any `AbstractSet`);
  - `bytes`, `bytearray` or `memoryview`;
  - a list or tuple inside a list or tuple.

  Encode such a value into text first, with `json.dumps` or the API's own format. A list or tuple of scalars still
  repeats its key, and a scalar httpx2 does not encode itself (a `UUID`, a `Decimal`, an `Enum` member, a `date`) is
  still sent as its `str()`.
- **`Paginated[I, P = object]` names the item type first.** Write `-> Paginated[Issue]` for a walk of issues, or
  `-> Paginated[Issue, IssuePage]` where the pages matter, in place of `Paginated[IssuePage, Issue]`. A type
  checker flags the old order wherever the page and item types differ.
- `paginate(max_pages=..., on_limit=...)` replaces `limit=PageLimit(...)`, which collided with every API's `limit`
  query parameter. Write `max_pages=10` for `limit=PageLimit(10)`, and `max_pages=10, on_limit="stop"` for
  `limit=PageLimit(10, on_limit="stop")`. `limit=` raises `TypeError`. `on_limit="stop"` without `max_pages` raises
  `ValueError`. A `max_pages` that is not an `int` (a `bool` included) raises `TypeError`, and one below 1 raises
  `ValueError`, before anything is sent.
- A `stream()` call's own read timeout may be longer than its deadline, which bounds only opening the stream. Once
  the head has arrived, only the per-read timeout bounds a stream, and `asyncio.timeout` around the block bounds the
  whole of it. `stream(..., deadline=5, timeout=Timeouts(read=60))` used to raise `ValueError`.
- **`AsyncBaseClient` is `Client`, which an SDK holds rather than inherits from.** `Client` is final: subclassing it
  raises `TypeError`. Write

  ```python
  class GitHub:
      def __init__(self, token: str) -> None:
          self._http = Client(base_url="https://api.github.com", auth=BearerAuth(token))

      async def repository(self, owner: str, name: str) -> Repository:
          return await self._http.get(t"repos/{owner}/{name}", expect=REPOSITORY)
  ```

  for `class GitHub(AsyncBaseClient)` calling `super().__init__(...)` and `self.get(...)`, and forward `async with`
  and `aclose()` to the client (docs/configuration.md shows the three methods). `with_options` returns a `Client`.
- A `ClientError`'s `source`, and the `[source]` prefix of its message, name the host of the client's `base_url`, with
  a port other than the default (`[api.github.com] ...`, `[localhost:8000] ...`), rather than the client's class. A
  token mint's failure still names the auth's class.
- `TestClock.install()` puts the clients built in its block on a fixed wall clock, `epoch` (2027-01-15 by default)
  plus the test clock's reading, as well as its monotonic clock. A test that builds a `Retry-After` date or a
  `RateLimit` reset from `time.time()` builds it from `clock.wall_clock()` instead.
- A retry whose wait the server asked for (`Retry-After`) draws no random number: only a backoff does, so a seeded
  `Timing(random=)` replays the same waits whatever the server asks.
- **Each client keeps its breakers to itself.** A client given no registry builds one of its own, so one client's
  failures no longer open a breaker another client is refused by. This changes behavior without an error: clients that
  relied on pooling their failures against an upstream must now be given one registry:

  ```python
  shared = BreakerRegistry()
  policy = TransportPolicy(breaker=BreakerPolicy(registry=shared))
  ```

  `BreakerPolicy(registry=)` defaults to `None`, the client's own. A registry keys a breaker by origin alone and fixes
  its thresholds the first time a client binds it: a client whose `BreakerPolicy` asks for other thresholds is refused
  with `ValueError` when it is built, instead of getting a second breaker for the origin.
  `headgate.health.breaker_states(registry)` takes the registry to report on.
- `RetryBudget(ratio=..., min_per_second=..., window=...)` as a policy value is `RetryShare(...)`, with the same
  fields and the same per-client meaning: a client keeps one budget per distinct share, spent by its calls, views and
  routes. `RetryBudget` is now live shared state, as `RateBudget` is: `RetryBudget(share, *, timing=None)`, and every
  client whose policy names the same object spends one window. `RetryPolicy(budget=)` takes a `RetryShare`, a
  `RetryBudget` or `None` (no cap). The old keywords raise `TypeError`, and `RetryBudget(clock=)` is
  `RetryBudget(share, timing=Timing(clock=...))`.
- Every clock is one `timing=`, and `None` reads the current timing when the object is built:
  - `BreakerRegistry(clock=c)` and `CircuitBreaker(clock=c)` are `timing=Timing(clock=c)`;
  - `RateBudget(wall_clock=w)` is `timing=Timing(wall_clock=w)`;
  - `Backoff(jitter=j)` is gone: the draw is the client's `Timing(random=j)`, and `Backoff.delay(retry, draw)` and
    `RetryPolicy.wait_after(retry_after, retry, draw)` take it;
  - `CachedTokenAuth(timing=)` is new.
- **`UpstreamStatusError.status_code` is `int | None`, a type change that ty and pyright flag wherever it is
  ordered.** A `RateLimitError` from the client's own rate budget, paused at the server's request past the call's
  deadline, carries `status_code=None`, and its message has no status tag, since the call got no response. Before, it
  carried the status of the earlier response that asked for the pause. A 429 or a refusal in a 200 body still carries
  its status. Code that compares `status_code` with `<` or `>=` narrows it first (`if exc.status_code is not None`);
  `==`, `in` and `match` need no change.
- `retryable` has one meaning on every error: sending the call again later could succeed, and is safe as the call
  stands. `CircuitOpenError.retryable` is now `True`, with the cooldown left as `retry_after`, as the rate budget's
  own refusals already were. The recipe in `docs/errors.md` reschedules it rather than re-raising it.
- Every error attribute is read-only: `source`, `UpstreamStatusError.status_code` and `.code`,
  `PageLimitError.pages`, and `RefusedError.message`, `.code` and `.kind` join the ones that already were. Assigning
  one raises `AttributeError`; build a new error with the value instead.
- `SecurityOptions(allow_private_hosts=False)` is `SecurityOptions(block_private_hosts=True)`, which says what it
  turns on without a double negative. The guard is still off by default. `allow_private_hosts=` raises `TypeError`.
- **A route's explicit `retry=None` now sends once, and its explicit `deadline=None` now sets no deadline.** Each
  meant "keep the policy's", and now means off, as a call's does: `Route` takes `**options: Unpack[RouteOptions]`, so
  leaving an option out inherits. Find them with `grep -rnE "Route\([^)]*(retry|deadline)=None"`, and leave the
  option out where you meant the policy's.
- **`RateRule` and `TransportPolicy(rate_limits=)` are gone: a path's budget is its route's `rate=`.** This is not
  silent: `rate_limits=` raises `TypeError`, so every configuration that used it has to be rewritten. Write
  `TransportPolicy(routes=(Route(fragment, rate=rate),))` for `rate_limits=(RateRule(fragment, rate),)`, merged into
  the route for that fragment if there is one. A call's first matching route is its route, whole: its retry, deadline,
  rate and caching come from that route alone, and what it leaves out from the policy and the client's budget, so a
  path that a route and a separate rate rule both matched now needs one route that sets everything. A route's
  `rate=None` paces nothing of its own there, while the server's pauses still apply; `cache=None` keeps its paths out
  of the cache. A redirect's later hops keep the call's route, and its budget, where a hop to another path could pick
  another rule. `Route(fragment, retry=False)`, which retried, raises `TypeError`, as do an unknown option and a
  `routes=` entry that is not a `Route`.
- `None` is the one spelling of off. `TransportPolicy(retry=NO_RETRY)` is `TransportPolicy(retry=None)`, which sends
  every call once unless the call gives a policy of its own. A client's `rate=` defaults to `None`, a budget that paces
  nothing of its own but still waits out the server's pauses, where it defaulted to `UNLIMITED`; write `rate=None`, or
  leave it out, for `rate=UNLIMITED`. `TransportPolicy(retry=False)` raises `TypeError`.
- `HttpOptions(timeout=...)` is `TransportPolicy(timeout=...)`, beside the `deadline` it is checked against, and a
  phase set longer than the deadline is refused where the policy is built, not in the client constructor.
  `HttpOptions(observers=..., middleware=...)` is `Client(..., observers=..., middleware=...)`: they are
  parts of the stack, not settings of how a request is sent. Each takes a sequence; a lone observer or a `str` raises
  `TypeError`. The old `HttpOptions` keywords raise `TypeError`.
- A walk of POSTs takes its body as `paginate(query=value)`, where it took `body=value` (encoded by the pager's
  `write=`) or `json=value` (builtins): the pager's `write=` encodes the value when it has one, and without one the
  value is sent as JSON. So `body=` means one thing everywhere, the request body. `paginate(body=...)` and
  `paginate(json=...)` raise `TypeError` naming `query=`, before the pager runs. The two `ValueError`s that tied a
  keyword to a pager having or lacking `write=` are gone, since the pager now decides; a fixed idempotency key on a
  walk of POSTs is still refused.
- A request body is one keyword, `body=`, whose value says what it is: `json=value` is `body=Json(value)`,
  `data=fields` is `body=Form(fields)`, `files=files, data=fields` is `body=Multipart(files, fields)`, and
  `content=raw` is `body=Raw(raw)`; `body=` an `EncodedBody` from a writer is unchanged. The old keywords raise
  `TypeError` naming the replacement, before anything is sent, on every verb and `stream()`. A call has one body, so
  the `ValueError`s for two that conflict are gone. `Json` is encoded as `json=` was, except that `Json(None)` sends
  `null` where `json=None` sent no body. A `Raw` content that is no body (a `str`, a `bytearray`, a list) raises
  `TypeError` when the `Raw` is built, and a `body=` that is no `Body` (bare `bytes`, a `dict`) raises `TypeError`
  naming it.
- **A call's or a view's explicit `retry=None` now sends once; it used to mean "take the view's or the client's
  policy".** `with_options(retry=None)` ignored the `None` and kept the client's policy; it now sends each of the
  view's calls once. `retry` is a call option now, where leaving it out inherits and `None` is off. A forwarding
  wrapper whose own default is `None` (`retry=retry`) is the usual case: type it on `**options: Unpack[CallOptions]`
  and forward `**options`, or leave `retry` out when it is `None`. Find affected calls and views with
  `grep -rnE "(retry|cache_ttl)=(None|[a-z_]+)\b"`.
- **A call's explicit `cache_ttl=None` now stores without a lifetime limit; it used to mean "the cache's `ttl`".** It
  removes headgate's lifetime only: the cache is TTL-only and still never reads a response's own cache headers, so such
  an entry is served until its storage evicts it, past any `Cache-Control: max-age` and even past `no-store`, as an
  entry of `HttpCache(ttl=None)` is. Leave `cache_ttl` out for the cache's `ttl`.
- `retry=`, `follow_redirects=` and `cache_ttl=` are no longer named parameters of the verbs, `stream()` and
  `paginate()`, but keys of their options (`CallOptions`, `RequestOptions`, `headgate.streaming.StreamOptions`), so a
  wrapper typed on the options forwards them. A call keeps the same spelling. `retry=False` is `retry=None`, on a call
  and on a view (`with_options(retry=None)`); `True` and `False` raise `TypeError` saying so. `follow_redirects=None`
  is leaving it out, and raises `TypeError`.
- A keyword no option names, such as a misspelled `retires=`, raises `TypeError` before anything is sent, on every
  verb, `stream()`, `paginate()` and `with_options()`, where it was ignored. `paginate(data=...)`, `content=` and
  `files=` now raise this `TypeError` too, naming the walk's `query=`, where they raised `ValueError`.
- `HttpOptions(refusal_check=...)` is `HttpOptions(response_check=...)`, and its type `RefusalCheck` is
  `ResponseCheck`. The check is unchanged: it sees the final response and returns a `RefusedError` or `None`. The
  `ValueError` for a redirect-following client names `response_check=`.
- `RefusedError` is imported from `headgate.errors`: write `from headgate.errors import RefusedError` in place of
  `from headgate import RefusedError`. It is still not a `ClientError`.
- `SigningAuth` signs every attempt, inside the retry layer, so a retry or a hedge carries a fresh signature instead of
  repeating the first. It still re-sends once on a 401, now signed afresh. A signature that must change per attempt
  belongs in `SigningAuth`, not in a middleware. A `SigningAuth` given to a bare `httpx2.AsyncClient`, or wrapped in
  another `httpx2.Auth`, now sends unsigned and does not re-send on a 401. A middleware now sees the request before it
  is signed, and one that replaces the request must carry `request.extensions` over, or that attempt goes unsigned.
- A middleware must declare `credential_headers` and `credential_params` (each a `frozenset` of names, empty when it
  stamps none). `HttpOptions` refuses one that does not, with a `TypeError`, since an undeclared or misspelled
  declaration sent the credential to every origin. A plain function is no longer a middleware; wrap it in a class with
  the two declarations.
- A call's URL is typed `LiteralString | Template | httpx2.URL`. A computed `str`, such as an f-string, no longer
  type-checks, because it skipped escaping: write a t-string for a path built from values, or `httpx2.URL(...)` for a
  URL taken from a response. To keep a prefix's structure, nest the prefix as a t-string (`t"{API_PREFIX}/items/{id}"`)
  or mark it `{prefix:raw}`; a bare `{prefix}` escapes its `/`. A `str` is still accepted at runtime.
- headgate requires hishel 2 (`hishel>=2.0.0`). `AsyncSqliteStorage(database_path=...)` is called as before, but
  hishel 2 cannot read a hishel 1 database: it raises on every lookup, so each call degrades to the network and nothing
  is cached. Point it at a new file, or delete the old one, when you upgrade. Its default file name is now
  `hishel_cache_v2.db`. A storage of your own implements hishel 2's `AsyncBaseStorage`
  (`create_entry(..., ttl=, tti=)`, `get_entries`, `update_headers`, `remove_entry`).
- The `caching` report hishel puts on a response says `stored: False` on a miss: headgate writes the entry only once
  the call accepts the response.
- Identical GETs asking for different `cache_ttl=` no longer share one download, which is stored for one TTL.
- `HttpCache(statuses=...)` may name only 2xx statuses other than 206, and raises `ValueError` for any other: hishel 2
  stores no other response. A declared non-2xx answer (`Expect(...).on(404, ...)`) is therefore never cached.
- hishel 2's own storages log and absorb their faults instead of raising, so `headgate.health.cache_degradations()`
  counts only faults a storage raises.
- A header set in `HttpOptions(headers=...)` is withheld from an origin that is neither `base_url`'s nor trusted, as
  the auth's credentials are, since a static key is often configured there. `User-Agent`, `Accept`,
  `Accept-Encoding` and `Accept-Language` are still sent. Add an origin to `trusted_origins` if it needs the rest.
- `x-should-retry: true` counts only on an error status: a 2xx or 3xx carrying it is the answer, never re-sent.
- A verb's `retry=` now defaults to `None`, meaning the view's setting. `retry=True` says what it reads as: undo the
  view's, so a call made on `with_options(retry=False)` with `retry=True` is retried under its route's policy or the
  client's. Before, `True` was also the default, so it could not override the view's `retry=False`, contrary to "a
  call's own options win over the view's".
- A reader is a `Reader`, a class: wrap a function of the response as `Reader(fn)`, or put `@Reader` over its `def`.
  `expect=`, `Expect(ok, answers)`, `Expect.on` and `conditional` raise `TypeError` for anything else, such as a model
  class (`expect=Order`, where `expect=reader(Order)` was meant), `str` or a bare function, and a type checker
  rejects them too. A reader's `repr` names its function, and `Reader` itself raises `TypeError` for something that
  cannot be called. `nothing`, `as_bytes` and `event_stream` are `Reader` values, and `body_reader` and `reader_for`
  return one, so code built on them is unchanged. `Expect(ok, answers)` whose answers read different types no longer
  infers their union: chain `.on()`, or spell the union out as
  `Expect[A | B | None]`. A refusal of the wrong kind now says what it got: `the class str`, `the type list[Order]`, or
  the value's type.
- The reader and writer builders share one vocabulary. Write `reader_for(codec, Type)` for `decoder_for(codec, Type)`,
  `writer_for(codec, Type)` for `encoder_for(codec, Type)`, and `headgate.msgspec.reader(Type)` and
  `headgate.msgspec.writer(Type)` for `headgate.msgspec.decoder(Type)` and `headgate.msgspec.encoder(Type)`, and the
  same for `headgate.pydantic`. A reader's refusal check is spelled `refuse=` where it was `refusal_check=`, on
  `reader_for`, the flavors' `reader` and `body_reader`. `Codec.decoder` and `Codec.encoder` keep their
  names.
- A call costs about 6% less CPU in headgate's own code. A client's timeouts are resolved once, not on every call,
  and fitting a call's timeouts to its deadline no longer sorts them. Behavior is unchanged.

### Removed

- `MAX_MINT_RESPONSE_BYTES`, which no option sets: a mint hop's response is read up to 1 MiB, as the auth guide says.
- `headgate.testing.faults`: import `FaultyTransport` and its steps from `headgate.testing`, which exports them all.
- The pagers and the finer errors from `headgate`: each has one home now. Import `BodyCursor`, `BodyOffset`, `Cursor`,
  `LinkHeader`, `NextUrl`, `Offset` and `PageNumber` from `headgate.pagination`, and `CircuitOpenError`,
  `ClientConfigurationError`, `DecodeError`, `ForbiddenDestinationError`, `PageLimitError`, `ResponseParseError`,
  `ResponseTooLargeError` and `Problem` from `headgate.errors`. `from headgate import Cursor` raises `ImportError`.
- `PageLimit`, from `headgate` and `headgate.pagination`: pass `max_pages=` and `on_limit=` to `paginate`.
- `headgate.breaker.DEFAULT_REGISTRY`, the process-wide registry: pass one `BreakerRegistry` to the clients that share
  it. `headgate.testing.isolated_breakers()`: each client's breakers are already its own; read them with
  `breakers_of(client)`.
- `RateRule` and `TransportPolicy.rate_limits`: write `Route(fragment, rate=...)` in `routes=`.
- `headgate.retry.NO_RETRY`: write `retry=None`. `headgate.ratelimit.UNLIMITED`: leave `rate=` out, or write
  `rate=None`.
- `OpenTelemetryObserver.install()`: pass the observer to the client twice, as `observers=(otel,)` and
  `middleware=(otel,)`, since it is both.
- `retry=True` on a call, which undid its view's `retry=`: make the call on the client instead of the view.
- `PageOptions`: `paginate()` takes `CallOptions`, which has the same keys plus `retry`.
- `HttpCache(policy=...)` and `HttpCache.filter_policy()`, and `headgate.cache.MethodFilter`, `PathFragmentFilter` and
  `StatusCodeFilter`: hishel 2 removed `FilterPolicy` and `BaseFilter`, which they were built on. Write the same rules
  as `HttpCache(methods=..., paths=..., statuses=...)`, which headgate now applies itself; read them back with the new
  `HttpCache.caches_request(method, path)` and `HttpCache.stores_status(status)`. A POST can no longer be cached:
  `FilterPolicy(use_body_key=True)` was the only way, and hishel 2 has no equivalent.
- `json_reader`: write `body_reader(decode, empty=b"[]")` for `json_reader(decode, allow_empty_body=True)`, and
  `body_reader(decode)` for `json_reader(decode)`, or read builtins with the new `as_json`.
- `json_writer`: write a function returning an `EncodedBody`, whose `content_type` defaults to `application/json`:
  `lambda value: EncodedBody(encode(value))` for `json_writer(encode)`.

### Fixed

- `TestClock.install()` reaches a `CachedTokenAuth`'s token lifetimes, a `RateBudget` shared between clients and a
  `BreakerRegistry`'s cooldowns, each built in the block. They read `time.monotonic()` themselves, so a test had to
  patch them. A `Retry-After` HTTP-date with no `Date` is read against the client's wall clock too.
- A hedge that loses while it is still above the rate budget, in a middleware that awaits before it sends, gives its
  retry budget back. The send it raced reached the rate budget meanwhile and marked the hedge as sent, so every such
  call kept one retry, and enough of them drained the budget: later calls could neither hedge nor retry.
- A rate budget hands a turn a caller abandoned in the middle of the queue (a cancelled call, or a deadline passing
  while it waited) to the next caller to arrive, while that turn's time is still ahead. The turn used to stay in the
  schedule unused, so a budget with callers giving up sent fewer requests than its rate.
- A `RateLimit` field whose `t` is a Unix time, which some servers send, pauses the budget until that time. It used to
  be read as that many seconds and cut to the hour-long maximum. `t` is now told apart by size, as
  `X-RateLimit-Reset` already was.
- `Rate` refuses an infinite request count, and one so small that the spacing between requests overflows, with
  `ValueError`. Either turned every send time into NaN, which paced nothing.
- `Route`s and the cache's `paths` fragments no longer match a `/` inside a t-string value: the path is
  split into segments before they are decoded, so `t"/files/{name}"` with `name="exports/q3"` sends
  `/files/exports%2Fq3`, which is not an `/exports` request. A regular expression in the cache's `paths` still
  searches the decoded path.
- Only responses from the client's own origin (`base_url`'s) pause and throttle its rate budget. A 429 or a spent
  quota from a redirect target, another origin's next page, a token host or an absolute URL on another origin paused
  the shared budget for every client using it, for up to an hour. Those requests are still paced, and each call still
  waits out its own `Retry-After`. A client always has an absolute `base_url`, so a client that calls absolute URLs
  on several origins is paused and throttled by its `base_url` origin's responses alone.
- A send that waited for its rate-budget turn while a hedge began marks its own attempt as sent, not the hedge's.
- A cached call is answered only by an entry stored for the same `Accept`, so a CSV and a JSON of one URL no longer
  answer each other. A request with a `Range` goes to the network, since no whole stored body answers it.
- Under `Partition.SHARED`, a call that sends `Authorization` (or a cookie, or a declared credential) is never answered
  by a stored entry that varies on it. An anonymous call's answer was served to an authenticated one.
- A send that waits for its rate-budget turn while a hedge starts beside it records its connection and the bytes it
  writes into its own attempt. They went into the hedge's attempt, whichever send wrote them.
- Every configuration value is checked for its kind where it is written, with a `TypeError` naming the option and what
  it takes, where some passed construction and failed at the first request or were misread:
  `TransportPolicy(idempotency=True)`, `routes=("/x",)`, a synchronous `HttpOptions(transport=)` and a `cache=` that is
  no `HttpCache` failed at the first request with an `AttributeError`; `request_id_headers="x-request-id"` and
  `trusted_origins="https://cdn.example"` were read as one name per character; and `deadline=True`,
  `max_redirects=True` and a `str` where a `bool` goes were taken as numbers or truths. A one-shot iterator where names
  go is refused, since checking it would empty it. Some values main converted are now refused, each with what to
  write: `HttpOptions(headers=)` takes a mapping, not a list of pairs; `max_response_bytes=` an `int`, not a float;
  `transport=` an `httpx2.AsyncBaseTransport` subclass, not a look-alike; and `follow_redirects=` a `bool`, not `1`.
- A problem+json error body with an extension nested hundreds of levels deep, about 1 KB, raised a raw
  `RecursionError` from every call. It is now read as no problem detail, and the status error names the body.
- An error body declaring `charset=idna` or `charset=undefined` raised a bare `UnicodeError`. It is read as UTF-8,
  as other charsets that are no text encoding already were.
- The default transport moves on to the next vetted address after a connect timeout, as it already did after a
  refusal. Each address gets an even share of the time left on the connect timeout, so one that hangs no longer
  spends all of it.
- A raw `#` in a URL's userinfo (`https://user:pa#ss@host/`) is redacted from error messages. The password after the
  `#` was shown.
- A `Retry-After` HTTP-date is measured from the response's `Date` header, so a local clock ahead of the server's no
  longer shortens the wait to nothing, and one behind no longer stretches it.
- An `Offset`, `PageNumber` or `BodyOffset` walk whose server ignores the paging parameter raises `ClientError` at
  the second page, which repeats the first page's body. Before, it fetched the same page forever.
- `Offset` starts at the offset the caller's `params=` give, as `PageNumber` starts at the caller's page, instead of
  overwriting it with `start`.
- `Offset(start=1, total=...)` no longer loses a 1-based walk's last item. With `total`, `Offset` and `BodyOffset`
  walk to the total even when the server caps its pages below `page_size`, ending early only at an empty page.
- A next-page URL that httpx2 cannot parse raises `ClientError`, not a bare `httpx2.InvalidURL`.
- `LinkHeader` skips a `rel="next"` link whose `anchor` names another resource. By RFC 8288 it is the next page of
  that resource, not of this one.
- A next-page URL that leads back to the walk's first page is caught one page sooner. The first page is now
  remembered by the URL it was sent to as well as by the relative URL it was written as.
- A walk remembers a 16-byte digest of each page's request, and keeps only the last 4096, instead of every request
  whole. A walk of large POST bodies held all of them.
- `Stream.aiter_lines` reads a body whose `charset` is no text encoding (`base64`, `rot13`, `idna`, `undefined`) as
  UTF-8, and raises `ResponseParseError` for a body its `charset` cannot decode (UTF-16 without a byte order mark).
  Each raised a raw `AssertionError`, `TypeError` or `UnicodeError`.
- An SSE `retry:` of any length of digits is read; one past 2**63 - 1 milliseconds is held at that. More than 4,300
  digits raised a raw `ValueError`, and a few hundred made `stream.retry / 1000` overflow.
- Whether an SSE event passes `max_event_size` no longer depends on how the body is cut into chunks: a comment line,
  or the byte order mark, still arriving counted toward it. A comment is now dropped as it arrives, so one that never
  ends holds no memory.
- A one-shot `content=` stream is never sent twice below the retry layer either. When an HTTP/2 GOAWAY moved the
  request to a new connection, an async-iterable file went out empty and the server processed an empty POST, and an
  async generator raised a raw `httpx2.StreamConsumed`. The second read is now refused, and the call raises
  `TransportError`.
- A `files=` upload of a file that cannot be rewound, such as a pipe or a socket, is sent once, like a one-shot
  `content=` stream. A retry or a followed 307 or 308 sent it again with an empty file. A file object counts as
  rewindable only if it is a regular file on disk or an in-memory file that can seek.
- A hedge sends its own copy of an in-memory body, and a streamed or multipart body is never hedged. A middleware that
  read `request.content` failed every hedged call with `RequestNotRead`, and two sends read one file at once.
- An empty or blank idempotency key is refused: `idempotency_key=""`, or an `IdempotencyKeys` factory returning one,
  raises `ValueError`. An empty `Idempotency-Key` header no longer makes a POST retryable.
- A `stream()` that became the half-open breaker's probe settles it at its response head. It used to hold the probe
  until its block exited, so every other call to that origin, from every client sharing the registry, was refused
  with `CircuitOpenError` for as long as the stream was read. A network failure mid-body now counts as one ordinary
  failure.
- A call admitted before its origin's breaker opened no longer changes the breaker when it finishes, even after the
  probe has closed it again, and its retries are refused once the cooldown elapses. Such a call failing late used to
  open the recovered breaker for another full cooldown, and its retries reached the origin beside the probe.
- A breaker transition listener (an observer of `BreakerChanged`) that raises when the probe is taken no longer leaves
  the breaker refusing every call forever.
- The call's deadline passing while `CachedTokenAuth` re-mints a token after a 401, or waits for another call's mint,
  is no longer charged to the API origin's breaker. Five such calls opened it for the whole process.
- A response a middleware answers itself, without sending the request, is no longer charged to the origin's breaker.
- `stream()` charges the breaker as a verb does: a stream that opens with a 4xx or a `response_check` refusal counts as
  the origin answering, and a network `TimeoutError` while reading the error's body is a transport failure rather than
  "deadline exceeded". A `ClientError` another call raises inside a stream's block is no longer reported as the
  stream's error, nor given the stream's notes.
- A cached client no longer buffers a `stream()`, a POST, or a request its `HttpCache` rules do not name: each goes to
  the network and up as it arrives, unshared. A server-sent event stream's block was entered only once the body ended
  or 32 MiB arrived. A stream is still served from a hit, but a stream's miss is no longer stored.
- On a cached client, a hedge sends its own request instead of waiting on its call's first send of the same GET.
- A cache write still running at a verb's deadline is dropped and counted in `cache_degradations()`; the call returns
  by its deadline. It ran on past it.
- A response header holding raw UTF-8 (`Content-Disposition: attachment; filename="café.csv"`) is handed up as sent.
  Every buffered call raised `UnicodeEncodeError`, and on a cached client a `Set-Cookie` beside it did too.
- Identical GETs share one download with `OpenTelemetryObserver` installed: its per-call trace headers no longer keep
  them apart.
- A client that refuses private hosts has cache entries of its own, so it is never served a body that another client
  on the same storage fetched from an internal address.
- A per-call `cache_ttl=` that is not a positive finite number raises `ValueError`, and one that is not a number
  `TypeError`, before the call is sent. `nan` was stored forever, and `"60"` was counted as a storage fault.
- A view's headers (`with_options(headers=...)`) partition the cache, as `HttpOptions(headers=...)` does. Two views
  with different `X-Api-Key` values on one client were served each other's cached entries.
- A view's headers are withheld from untrusted origins, as client-wide ones are, and a call's own `headers=` from a
  redirect or a later page that leads to another origin than the one the call named. Both reached any origin a
  redirect or a `Link` header named.
- A next page an `https` page names on plain `http` raises `ForbiddenDestinationError` before it is sent, as a
  downgrading redirect is refused. It was sent in cleartext.
- A body nested too deeply for its parser (around 100,000 levels for `json.loads` and msgspec) raises
  `ResponseParseError` on a 2xx, or the status's error on a declared non-2xx, where it raised a raw `RecursionError`,
  whichever reader read it: a codec's, `as_json`, `body_reader(json.loads)` or a hand-written reader over
  `response.json()`. A token mint that meets one raises `ResponseParseError` too. The message ends `nested too deeply`,
  whichever reader and parser met it.
- A cached response still honors `Vary`: hishel 2 keys an entry by its URL alone, so headgate matches the headers a
  response varies on itself. A response with `Vary: *` is no longer stored, since nothing could match it.
- Under `Partition.SHARED`, a response varying on a header the cache never stores (`Authorization`, a cookie, a
  declared credential) was served to every other caller, whatever that header said. It is no longer stored.
- `post(json=..., data=...)` raises `ValueError`. httpx2 sent the form alone and dropped the JSON without a word.
- A bare number or `None` where an option takes a value object is refused with a `TypeError` naming the option and
  what it takes: `rate=10` (a `Rate` or `RateBudget`), `TransportPolicy(retry=None)` (turn retries off with
  `NO_RETRY`), `TransportPolicy(breaker=3)`, and `timeout=5.0` on `HttpOptions`, `with_options` or a call. Each passed
  construction and failed at the first request with an `AttributeError`.
- Concurrent GETs that a middleware gives different timeouts no longer share one download. When a shared download
  times out in a known phase, a waiting GET fetches for itself only if that phase was one the first request's deadline
  shortened, rather than if any phase was. A timeout of no known phase still hands off when any phase was shortened.
- The [standards page](https://kelsayed17.github.io/headgate/standards/) now lists the two RFCs headgate follows but
  left out: RFC 10008 (a 302 keeps a QUERY's body, so a one-shot body is refused there) and RFC 3879 (the SSRF guard
  refuses IPv6 site-local addresses). It also names the RateLimit draft revision headgate reads, -11.
- Twenty exported names appeared in no guide, among them `RequestOptions`, `PageOptions`, `ViewOptions`, the SSRF
  guard's `AddressGuard` and `PinnedHTTPTransport`, `RETRY_STATUSES` and `testing.Reply`. Each is now described where
  it is used, and a test fails when an export, or an RFC the source cites, is missing from the docs.
- A client-wide header such as `X-Api-Key` was sent to another origin on a cross-origin redirect or an absolute
  next-page link.
- The HTTP cache served one caller's response to another when a call set its own value for a credential the auth
  declares (an `Authorization` with `BearerAuth` or `CachedTokenAuth`, an `api_key` with `QueryParamAuth`). That value
  now goes into the partition.
- A `DecodeError` or `ResponseParseError` chained the codec's own error, which quotes the input, so a traceback or
  `logger.exception` logged the body's values. They are raised without a cause, and `check_codec` checks the traceback.
- A hedged call hung when its rate budget's `max_in_flight` was full: a failed send kept its slot while the hedge
  waited for it. An answer arriving as the hedge timer fires no longer spends a retry, and a hedge cancelled before it
  is sent gives its share back.
- A call ended by its deadline while waiting on the origin never counted against the circuit breaker, so an origin that
  accepts connections and never answers was never isolated. It now counts as a timeout, and a failure the fetch
  marked counts however the block exits.
- A POST answered `201` with `x-should-retry: true` was sent again.
- `paginate` refused one `Idempotency-Key` for every page only when the call set it, not a view or the client.
- Loop detection in a walk of POSTs compared the body's `repr`, which for an object without a value `repr` is its
  address: a false "pagination loop", or a missed one. It compares the bytes sent.
- An error response with a `charset` that names no text encoding (`base64`) raised a bare `LookupError`.
- With an observer attached, a malformed `Link` on a deprecated response turned a success into a `ClientError`.
- The pydantic encoder's `drop_none=True` kept a `None` inside a nested dict, unlike `to_builtins` and msgspec.
- `testing.Respond(retry_after=1e6)` wrote `1e+06`, which no parser reads; it writes plain digits and refuses a
  negative or non-finite value.
- A deadline message wrote a long elapsed time in scientific notation (`1.23e+03s`).
- Every call left its context in a reference cycle (through the retry verdict's judge), so what it allocated was
  freed only at the next garbage collection, raising the memory a stream of calls held at its peak.
- A `SigningAuth` signs each attempt after the rate-limit wait, just before it is sent. It signed before the wait, so
  a request queued behind a tight budget went out with an old signature: at 1 request a second against a server that
  refuses signatures older than 30 seconds, a third of 45 concurrent calls took a 401 and spent their budget twice.
- A client-wide `Content-Type` no longer replaces the one `json=`, `data=` or `files=` encodes the body with. A
  multipart upload lost its boundary and could not be parsed. A call's own `headers=` still wins.
- `Content-Type` is sent to an untrusted origin. A client-wide or view `Content-Type` was withheld there, so the body
  went out with no type.
- A credential declaration that is not a frozenset of names is refused with a `TypeError`: `token_headers`,
  `token_params`, `signed_headers` and `signed_params` when the class is defined, and an auth's `credential_headers`
  and `credential_params` when the client is built. A bare `str` (`"x-api-key"`) iterates as its letters, so the
  guard and the cache withheld single letters instead of the header.

- `AsyncCurlTransport` sends a request the retry layer would not repeat (a POST or PATCH without an idempotency key,
  or any method outside the call's `RetryPolicy(methods=...)`) on a fresh connection. On a reused keep-alive
  connection that died unanswered, libcurl sent it a second time on its own, so the server could process it twice
  while headgate counted one attempt.
- A call through `AsyncCurlTransport` cancelled before the response head, by a deadline or a timeout, ends its
  transfer and gives its connection back. curl kept downloading the body into memory nobody read, outside
  `max_response_bytes`, and enough such calls held every connection, so calls to healthy paths timed out.
- An `AsyncCurlTransport` response is returned at its head, as httpx2's own transport does, not at its first body
  byte, so a stream whose first event is slow no longer times out on open.
- Closing an `AsyncCurlTransport` response whose body has stalled returns at once. It waited for curl's own stall
  timeout, past the call's deadline.
- An explicit `CurlOpt.PROXY` in `AsyncCurlTransport(curl_options=...)` is used for every host. The transport's
  `NOPROXY="*"`, which keeps the environment's proxy out, bypassed it too; a `CurlOpt.NOPROXY` of your own is kept.
- A 401 that `SigningAuth` or `CachedTokenAuth` answers by re-sending the request is dropped unread. httpx2 read it in
  full, below `max_response_bytes`: a 66 MiB 401 body was read under a 1 MiB cap.
- A token response that `MAX_MINT_RESPONSE_BYTES` refuses by its `Content-Length` closes its connection. It was left
  open, so each such mint kept a pooled connection until the pool ran out.
- When a token mint fails, every call that was waiting for it fails with the same error. Each waiting call used to
  try the mint again, one after another, so a token host that was down cost one timeout per queued call.
- A token refresh that fails inside `refresh_margin` sends the held token, which has not expired yet, instead of
  failing the call.
- A `mint_flow` hop that fails on the network (a refused connection, a timeout) is reported as the token mint's
  `TransportError`, named for the auth class and the token endpoint. It was reported as the API call's, naming the
  call's URL.
- Under a headgate client, `CachedTokenAuth` no longer reads a streamed upload into memory before sending it. A body
  that can be sent again (bytes, a factory, a rewindable `files=` upload) is still re-sent after a 401 re-mint; a
  one-shot stream's 401 reaches the caller as `AuthError`, and the next call mints a new token.

- `AsyncCurlTransport` reads a request's body from its stream on every send, as httpx2's own transport does. It read
  the body once and sent that copy again, so a one-shot body sent a second time was not refused.
- `FakeTransport.calls` logs a snapshot of each send, so a request changed between attempts shows each attempt's own
  headers; it logged the one live request object, so every attempt showed the last one's. A reply function is given
  the snapshot.
- `FakeTransport` reads a request's body from its stream on every send, as a network transport does. It read the body
  once and served that copy again, so a one-shot body sent twice went unnoticed in tests.
- `check_codec` refuses a codec that reads a numeric string (`"1"`) as an `int`. It caught only strings that are no
  number.
- `CassetteFilter` keeps credentials out of recorded responses too: it drops the headers it filters from requests,
  besides `Set-Cookie`, and redacts the secret query values in a `Location` or `Content-Location`, such as a signed
  download URL.

### Security

- `AsyncCurlTransport(curl_options={CurlOpt.FOLLOWLOCATION: True})` raises `ValueError`. curl followed each redirect
  itself, below every headgate hook, so the credential guard never saw the hop and a credential went to whatever
  origin the redirect named. headgate follows redirects itself (`HttpOptions(follow_redirects=...)`).
- `AsyncCurlTransport` verifies TLS against the Mozilla CA bundle `curl_cffi` installs. It took its trust store from
  `SSL_CERT_FILE`, `CURL_CA_BUNDLE` or `REQUESTS_CA_BUNDLE`, which a native transport ignores; pass `CurlOpt.CAINFO`
  in `curl_options` for a bundle of your own. libcurl still writes TLS session keys to the file `SSLKEYLOGFILE` names,
  when it is set.
- A transport or token-mint error whose message quoted one long word (a redirect to a URL with a 64 KiB port, a token
  field the mint's parser quoted whole) took time quadratic in the word's length to redact, over twenty seconds on a
  desktop for the longest URL httpx2 accepts, blocking the event loop on a value the server chose. The `name=value`
  pattern now makes one full attempt per word, in linear time, and redacts exactly the pairs it did before.
- A password with a bare `@` in it (`https://user:p@ss@host/`) is redacted whole from an error message. The redaction
  of a URL's userinfo in free text stopped at the first `@` and left the rest of the password in the message.
- A `CachedTokenAuth` whose `attach` put the token in the query had no way to declare it, so the token reached any
  origin a redirect or a next-page link named, and a 401 never re-minted. Such a subclass declares `token_params`; a
  custom `attach` whose class declares no header and no parameter fails when it is defined.
- httpx2's own `HTTP Request:` log line (INFO, `httpx2` logger) printed the URL as sent, a `QueryParamAuth` key or a
  `SigningAuth` query signature included. Importing headgate adds a filter to that logger that redacts the URL, as
  every error message is redacted. headgate still writes no log records of its own.
- A failed token mint is raised without a cause. It chained httpx2's own error, whose message names the token URL
  unredacted, so a traceback or `logger.exception` logged a `client_secret` in the URL. A note names the original
  error's type and its redacted message instead.
- When the cache storage failed and the live fetch that replaced it failed too, the live error carried the storage
  error as its context, and a traceback printed the backend's message, which can hold a DSN and its password.

## [0.11.0] - 2026-11-02

What an API client wrote around headgate is now headgate's: a pager over numbered pages, an offset walk that ends at
an exact total, a base class for signed requests, cache paths matched by pattern, and a public way for a client's tests
to read back the cache and rate budgets it was built with. Nothing existing changes behavior; this is a minor release
because it adds to the public API.

### Added

- `PageNumber(items=, more=, param="page", start=1)`, a pager over numbered pages: page *n* + 1 follows page *n* on
  the caller's own request while `more(page)` says another follows. The walk starts at the page the caller's
  `params=` name, and reads a page's `next` link (or count, or flag) only as a yes or no, so every page keeps the
  caller's URL and query. See [Strategies](docs/pagination.md#strategies).
- `total=` on `Offset` and `BodyOffset` reads the exact number of items the walk holds from a page, and ends the walk
  once the offset reaches it, instead of asking for the empty page past a full last page. A `total` of `None` leaves
  the walk to end at a short page.
- `SigningAuth`, the base class of an auth that signs every request with a provider's signer (a JWT bound to the
  method and path, an HMAC or Ed25519 signature over the body). A subclass supplies `sign(request)` and declares what
  it sets in `signed_headers` or `signed_params`; the base class reads the body first, leaves a request the caller
  signed alone, signs a 401's request again once (a retry re-sends the first, possibly expired, signature), and
  reports what it sets to the credential guard and the cache. See [SigningAuth](docs/auth.md#signingauth).
- `HttpCache(paths=)` and `PathFragmentFilter` take compiled patterns beside whole-segment strings. A pattern is
  searched for in the URL path, so a cache can keep what segments cannot name, such as a file name's prefix or
  extension (`re.compile(r"/fut_fin_txt_\d{4}\.zip$")`). A pattern that matches an empty path raises `ValueError`, as a
  fragment naming no segment does. See [What is stored](docs/caching.md#what-is-stored-and-for-how-long).
- `headgate.testing.cache_of(client)` and `budget_for(client, url=None)` read back what a client was built with: the
  `HttpCache` (or `None`), and the `RateBudget` a request to `url` spends from (the default budget without one). A
  client's tests no longer import `headgate.cache._policy` or `headgate.ratelimit._budgets`, or walk its transport
  stack. See [What a client was built with](docs/testing.md#what-a-client-was-built-with).
- `HttpCache.filter_policy()` returns the hishel `FilterPolicy` the cache stores by: its own `policy`, or the one built
  from `methods`, `paths` and `statuses`.
- `headgate.DEFAULT_TIMEOUTS`, the timeouts of every phase a client leaves unset, is public, so a deadline computed from
  a client's timeouts reads the real defaults. See [Timeouts](docs/deadlines.md#timeouts).

## [0.10.0] - 2026-11-02

A client whose wire is not JSON names its reader on one line: `body_reader` builds a reader from a bytes decoder in
any format. Nothing existing changes behavior; this is a minor release because it adds to the public API.

### Added

- `body_reader(decode, *, empty=None, refusal_check=None)` builds a reader from a bytes decoder in any format (an
  `.xls` workbook, a fixed-width TXT, a CSV), so an endpoint names its reader on one module-level line instead of
  writing a function around the decoder. `json_reader` and every codec's `decoder` build on it. See
  [Readers](docs/responses.md#readers).

### Fixed

- The guides and docstrings say what 0.9.0 does. `Idempotency-Key` is opt-in, not sent by default. The options that
  cannot be combined with `transport=` are `ssl_context`, `limits` and `proxy`. msgspec leaves out what the body chose
  rather than showing `[...]`. `Rate.per_second(5)` and `Rate.per_minute(300)` share a spacing, not a burst.
  `TestClock` moves after 16 quiet turns of the event loop. A pause past the deadline on a retry ends the call with its
  last failure. A body's `Content-Type` and an `Expect`'s headers rank above the client's default headers. And a token
  mint redirected off its origin raises `ForbiddenDestinationError`.
- The mutation-testing page reports a run on 0.9.0: 6,649 mutants, 97.6% detected, and every survivor equivalent and
  listed with its reason. The run's 76 survivors that changed behavior each now fail a new test.

## [0.9.0] - 2026-10-27

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
- `typing-extensions` is a dependency on every Python version, not only before 3.15: `RequestOptions` and
  `PageOptions` are its closed `TypedDict`.

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

- `headgate.ratelimit.BudgetBacklogError`, raised by `RateBudget.wait` when a call's paced turn comes after its
  deadline.

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

[Unreleased]: https://github.com/kelsayed17/headgate/compare/v0.12.1...HEAD
[0.12.1]: https://github.com/kelsayed17/headgate/compare/v0.12.0...v0.12.1
[0.12.0]: https://github.com/kelsayed17/headgate/compare/v0.11.0...v0.12.0
[0.11.0]: https://github.com/kelsayed17/headgate/compare/v0.10.0...v0.11.0
[0.10.0]: https://github.com/kelsayed17/headgate/compare/v0.9.0...v0.10.0
[0.9.0]: https://github.com/kelsayed17/headgate/compare/v0.8.0...v0.9.0
[0.8.0]: https://github.com/kelsayed17/headgate/compare/v0.7.0...v0.8.0
[0.7.0]: https://github.com/kelsayed17/headgate/compare/v0.6.0...v0.7.0
[0.6.0]: https://github.com/kelsayed17/headgate/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/kelsayed17/headgate/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/kelsayed17/headgate/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/kelsayed17/headgate/compare/159a3c2...v0.3.0
[0.2.0]: https://github.com/kelsayed17/headgate/compare/f319ad8...159a3c2
[0.1.0]: https://github.com/kelsayed17/headgate/commit/f319ad8
