# Design

How headgate is built, for contributors and for anyone deciding whether to trust it with their clients. The user guides
describe what each piece does; this describes how they fit together.

## Principles

1. **An endpoint method stays one line.** `return await self.get(t"series/{id}", expect=SERIES)`. Every capability is
   configured once on the client, in an option object, or is a value passed at the call. No endpoint repeats
   boilerplate, so no endpoint can get it wrong.
2. **Composable, typed, frozen values over flags.** Policies are frozen, slotted, keyword-only dataclasses that validate
   in `__post_init__` and freeze the collections they hold (`tuple`, `frozenset`, `MappingProxyType`).
3. **One place per concern.** Each policy lives in one module or subpackage. `_client.py` orchestrates a call and owns
   no policy logic; `_stack.py` assembles the layers.
4. **Safe by default, explicit to loosen.** Credentials, cache sharing, redirects, retries of writes, the environment
   and cookies all default to the safe behaviour; loosening each is a named option.
5. **No logging, no environment reads.** Observability is typed events delivered to observers the caller installs.
   Nothing reads `os.environ`; the HTTP client and the default transport are built with `trust_env=False`.
6. **Injectable time and randomness.** Every clock, sleep and random source is a parameter with a production default.
   `headgate.testing` supplies fakes; no test patches a private global.

## Packages and the import boundary

```text
src/headgate/
  __init__.py        the public API, re-exported
  _client.py         AsyncBaseClient: a call end to end
  _stack.py          the transport stack, assembled in one place
  _options.py        TransportPolicy, HttpOptions, Timeouts, IdempotencyKeys, Route
  _responses.py      Reader, Expect, RefusedError and the built-in readers
  _failures.py       the ClientError a failed exchange becomes
  _auth.py           StaticHeaderAuth, QueryParamAuth, CachedTokenAuth
  _codec.py          the Codec protocol, and the readers and writers built on one
  _body.py, _request_body.py, _redirects.py, _middleware.py, _attempts.py, _deprecation.py, _view.py, _url.py,
  _user_agent.py, _curl.py
  _core/             shared seams only: time, origins, CallContext, events, the policy switch
  retry/             RetryPolicy and its values, RetryTransport, send tracking, the budget ledger
  breaker/           BreakerPolicy, BreakerRegistry, CircuitBreaker
  ratelimit/         Rate, RateBudget, Adaptive, RateRule, RateLimitTransport, server feedback
  cache/             HttpCache, partitions, the resilient cache transport, conditional reads
  security/          the credential guard, the https-downgrade refusal, the SSRF guard
  streaming/         Stream
  pagination/        Pager, its strategies, Paginated
  errors.py, events.py, health.py, policy.py, otel.py, curl.py, msgspec.py, pydantic.py, testing/
```

`retry`, `breaker` and `ratelimit` may import only the standard library, `httpx2`, their own subpackage and
`headgate._core`; `_core` only the standard library and `httpx2`. They never import the client, the stack, the cache or
each other, so any of them could ship as a distribution of its own. Where they must cooperate (the retry layer asking
whether the breaker opened, the rate budget and the retry layer reading `Retry-After` the same way) they meet at a
seam defined in `_core` and wired in `_stack.py` or `_client.py`. `tests/package/test_boundaries.py` parses every
import, including those under `TYPE_CHECKING`, and fails on a crossing.

The codecs are the other boundary: the core imports no serialization library at runtime, and
`tests/package/test_import_isolation.py` checks it. It reaches one only through the `Codec` protocol
(`headgate/_codec.py`), which `headgate.msgspec` and `headgate.pydantic` implement and any library can.

## A call, top to bottom

```text
AsyncBaseClient.get(url, expect=..., **options)
  _prepare           render the t-string; build the httpx2.Request once; stamp its CallContext
  _reported          CallStarted ... CallFinished; the call's notes added to the ClientError it raises
  _deadline          asyncio.timeout(time left) around everything below
  _exchange          the origin's breaker: admission, then track() around the send and the body read
    httpx2.AsyncClient.send
      auth flow                  once per call; a mint's hops are sent through this same stack
      request hooks              every hop: the credential guard; the SSRF check over a custom transport
      RetryTransport             attempts, classification, backoff, Retry-After, budget, hedging
        AttemptTransport         AttemptFinished per send (only with observers)
          MiddlewareTransport    HttpOptions.middleware, once per attempt, then the credential guard again
            ResilientCacheTransport   opt-in hishel cache; a hit stops here
              RateLimitTransport      the rate budget: pacing, in-flight slots, server feedback
                SendTrackingTransport whether each send connected, and the request bytes it wrote
                  network transport   HTTP/2 AsyncHTTPTransport (pinned under the SSRF guard), or HttpOptions.transport
      response hooks             every hop: refuse an https downgrade; refuse to re-send a one-shot body; drop a
                                 followed redirect's body
  _sent              a surviving 429/5xx raised; the breaker charged with what the exchange showed
  read_capped        the body, up to max_response_bytes, decompressed bytes counted
  _dispatch          refusal_check, then the declared answers, then the status ladder
```

`stream()` takes the same path, except that the response is handed to the caller's block open, with the breaker scope
held until the block exits, and the deadline bounds only opening it. Each page of `paginate()` is a `get`.

The breaker and the deadline are driven by the client, not by a transport layer, because only the client's scope spans
both the send and the body read. The cache is below the retry layer, so a hit is never retried, and above the rate
budget, so a hit spends nothing. Middleware is inside the retry layer, so it runs per attempt, and above the cache, so it
sees cache hits too. `build_transport_stack` builds the layers bottom-up, one statement each.

## CallContext

A request travels through httpx2, hooks, a cache that rebuilds it, and transports that know nothing of each other. The
per-call state they share rides on the request as a single extension, a `CallContext`, which httpx2 copies to every
redirect hop and the cache layer restores below hishel. Every layer finds it with `CallContext.of(request)`:

- what the call is: `info` (method, sanitized URL, route template, absolute URL, call id);
- when it must end: `deadline`, absolute on the client's clock, `remaining()`, and `fit_timeouts(request)`, which cuts a
  send's timeouts to the time left;
- its sends: `begin_attempt()`, `attempt`, and the current attempt's `progress` (connected, bytes sent);
- what it carries: `idempotency_key`, `follow_redirects`;
- what happened: `add_note()` lines, which the client adds to the error, and `emit()` for events;
- per-layer data: a layer that needs a value of its own declares a typed `Slot` in its own module and uses `get` and
  `put`. `RETRY_POLICY`, `REPLAYABLE_BODY` and `RETRY_OUTCOME` (in `retry`) and `BREAKER_GATE` (in `_core`) are the slots
  today.

A request no client call built (a test driving one transport, a hop an auth flow yields) gets a standalone context
with no deadline.

## Events

The event types are defined in `_core`, below every layer, so each layer emits the events it owns through the call's
context: the client emits `CallStarted`, `CallFinished`, `BreakerChanged` and `Deprecated`; the attempt layer
`AttemptFinished`; the retry layer `RetryScheduled`; the rate budget and its transport `RateLimited`; the cache
`CacheResult`. A layer checks `context.observed` before building one, so a client without observers pays nothing.
`Emitter` delivers to each observer in turn, swallowing and counting what one raises.

## Adding a layer

1. Write it as an `httpx2.AsyncBaseTransport` that wraps the transport below it and closes it in `aclose()`
   (`httpx2.AsyncBaseTransport.aclose` is a no-op, so a missing override leaks the pool).
2. Decide where it sits: above the retry layer it runs once per call; below it, once per attempt; above the cache, for
   hits too; below the rate budget, only for what reaches the network.
3. Configure it with a frozen option value on `TransportPolicy` or `HttpOptions`, validated in `__post_init__`.
4. Keep its per-call state in a `Slot` on the `CallContext`, never in a request extension of its own, and its waits on
   the injected `Timing`.
5. Emit a typed event for what an operator would want to see, with sanitized data only, after checking
   `context.observed`.
6. Add one statement to `build_transport_stack`, in its place.
7. Cover every branch, add its invariants to the property tests where it touches retries, and update the snapshots it
   changes deliberately (`tests/package/test_api_surface.py` pins the public API; `tests/observe` pins event sequences
   and spans).

## Verification

The suite runs at 100% line and branch coverage with no suppression comments. Beside behaviour tests per area:

- **property tests** (Hypothesis) drive the retry layer over random `FaultyTransport` schedules, checking that a write
  without an idempotency key is never re-sent after a byte was written, that no attempt or wait outlasts the deadline,
  that retries stay within the budget, that every response not returned is closed, and that the error's history matches
  what was sent; a state machine drives the breaker through concurrent calls and clock jumps;
- **integration tests** exercise the seams between areas: the breaker gate in the retry loop, one deadline across every
  layer, pagination and streams under the credential and SSRF guards, the cache over the real auths;
- **snapshots** (syrupy) pin the public API's signatures, error messages, event sequences and OpenTelemetry spans;
- **benchmarks** (pytest-codspeed) and deterministic memory tests guard the hot paths, and mutation testing (mutmut)
  checks that the tests notice a changed line (see [mutation testing](mutation-testing.md) for how to run it, the latest
  score and the equivalent mutants).
