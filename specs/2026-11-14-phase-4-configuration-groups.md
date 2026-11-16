# Phase 4: Configuration groups, "off" and routes — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Put each setting in the group it is for, give "off" one spelling, `None`, in every configuration value and override, and make one per-path rule, `Route`, the only one. Finish the construction-time kind checks, so a value of the wrong kind fails where it was written and not at the first request.

**Architecture:**
- **Groups.** `timeout` moves from `HttpOptions` to `TransportPolicy`, beside the `deadline` it is checked against. `observers` and `middleware` move from `HttpOptions` to the client constructor, since they are parts of the stack, not settings of one layer.
- **"Off".** `TransportPolicy(retry=None)` sends once, and the client's `rate=None` paces nothing of its own. `NO_RETRY` and `UNLIMITED` stop being public; each survives only as a private constant the stack uses for "off".
- **Routes.** `Route(fragment, /, **options: Unpack[RouteOptions])` takes `retry`, `deadline`, `rate` and `cache`. An option left out inherits, and `None` is off. The first matching route is the call's route as a whole, and one function picks it for the retry, deadline, rate and cache layers alike. `RateRule` and `TransportPolicy.rate_limits` are removed.
- **Rename.** `SecurityOptions(allow_private_hosts=False)` becomes `SecurityOptions(block_private_hosts=True)`.

**Tech Stack:** Python 3.14, httpx2 2.16, ty, ruff, pytest, syrupy (API snapshot).

**Spec:** `specs/2026-11-05-public-api-redesign-design.md`, section 4. Phases 1–3 merged as #56, #60 and #85. From the November codebase review's plan, this phase also finishes the construction checks of finding M22, the ones Phase 3 did not cover.

## Global Constraints

- Python 3.14 or newer; httpx2 2.16 or newer; hishel 2.
- Base branch: `origin/main` at or after `ae4f551` (#85).
- Clean break: no deprecation aliases, and no old spelling kept next to the new one. A removed keyword raises `TypeError`, because a frozen dataclass refuses an unknown field and `Route` refuses an unknown option. A removed name raises `ImportError`.
- Gates for every task:
  - `uv run ruff check`
  - `uv run ruff format --check`
  - `uv run ty check`
  - `uv run typos`
  - `uv run python -m pytest --cov` at 100% line and branch coverage
  - No `noqa`, `ty: ignore` or `pragma: no cover`.
- A change to the public API updates the snapshot with `uv run python -m pytest tests/package/test_api_surface.py --snapshot-update`, and the diff is reviewed.
- Every exported name appears in `docs/`, and a test enforces it.
- American English in code, docs and messages.
- Each task adds a `CHANGELOG.md` entry under `## [Unreleased]`'s existing **Added**, **Changed** or **Removed** sections. Each change or removal says what to write instead.
- Docstrings describe what the code does now, in the present tense.
- The property tests in `tests/retry/test_properties.py` keep their properties and assertions. Only their spellings change (`NO_RETRY` → `None`, `rate_limits=` → `routes=`).
- The verb-path benchmarks (`benchmarks/test_client.py`) show no regression against `origin/main`.
- Out of scope:
  - `RetryBudget` as shared state, the `timing=` seam, `Client` by composition, and the breaker registry (phase 5).
  - Errors (phase 6).
  - `Timeouts.UNBOUNDED` and `max_pages=` (phase 7).
  - Matching a route by t-string template (see **Decisions**).
  - The top-level trim (phase 8): a name this phase adds is exported where its neighbors are.

## Coordination with round 2

Three round-2 fixes change lines this phase rewrites. Rebase onto each as it merges; none changes the design here.

- **M11, the rate budgets.** `PathBudgets(...)` in `AsyncBaseClient.__init__` gains `origin=base_url`, so a budget takes server feedback only from the client's own origin. Task 4 rebuilds `PathBudgets` from routes and must keep `origin=` and what it does.
- **L3, `%2F` inside a path segment.** `match_route(routes, request.url.path)` and `HttpCache.caches_request(method, path)` change to take the URL, so an escaped `/` cannot pick another path's rule. Task 4 calls `match_route` from the rate layer and the cache layer too, so every caller uses the URL form once L3 lands.
- **L14, auth.** One new branch at the top of `_network_failure`. That function is not touched here.

**Pagination:** this phase does not touch `pagination/`. `paginate` takes `CallOptions` as before, and each page, a call of its own, is matched to its route by its own URL, as today. `PageLimit` and `limit=` wait for phase 7. So the pagination fixes (M16, L19–L21, audit items 17 and 18) can proceed at any time.

## Public signatures after this phase

Everything not shown is unchanged.

```python
# headgate (top level)
class AsyncBaseClient:
    def __init__(
        self, *, base_url: str,
        rate: Rate | RateBudget | None = None,            # was Rate | RateBudget = UNLIMITED; None: no pacing of its own
        auth: httpx2.Auth | None = None,
        policy: TransportPolicy = ...,
        http: HttpOptions = ...,
        observers: Sequence[Observer] = (),                # moved from HttpOptions
        middleware: Sequence[Middleware] = (),             # moved from HttpOptions
    ) -> None

@dataclass(frozen=True, slots=True, kw_only=True)
class TransportPolicy:
    retry: RetryPolicy | None = RetryPolicy()              # None: send once (was NO_RETRY)
    timeout: Timeouts = Timeouts()                         # moved from HttpOptions
    deadline: float | None = 60.0
    breaker: BreakerPolicy | None = BreakerPolicy()
    cache: HttpCache | None = None
    idempotency: IdempotencyKeys | None = None
    routes: tuple[Route, ...] = ()
    # rate_limits: removed

class RouteOptions(TypedDict, total=False, closed=True):
    retry: RetryPolicy | None                              # None: send once on this path
    deadline: float | None                                 # None: no deadline on this path
    rate: Rate | RateBudget | None                         # None: no pacing of its own on this path
    cache: None                                            # None: this path is never cached

@final
class Route:
    def __init__(self, fragment: str, /, **options: Unpack[RouteOptions]) -> None
    fragment: str                                          # read-only
    options: Mapping[str, object]                          # read-only: what the route sets, and nothing it left out

@dataclass(frozen=True, slots=True, kw_only=True)
class HttpOptions:                                         # loses timeout, observers and middleware
    ...

@dataclass(frozen=True, slots=True, kw_only=True)
class SecurityOptions:
    trusted_origins: frozenset[str] = frozenset()
    block_private_hosts: bool = False                      # was allow_private_hosts: bool = True
    resolver: Resolver | None = None                       # only with block_private_hosts=True
```

Removed:
- `headgate.RateRule` and `headgate.ratelimit.RateRule`;
- `headgate.ratelimit.UNLIMITED`;
- `headgate.retry.NO_RETRY`;
- `TransportPolicy.rate_limits`;
- `HttpOptions.timeout`, `HttpOptions.observers` and `HttpOptions.middleware`;
- `SecurityOptions.allow_private_hosts`;
- `OpenTelemetryObserver.install()`, see **Decisions**.

## Migration

| Before | After | The old spelling now |
| --- | --- | --- |
| `TransportPolicy(retry=NO_RETRY)` | `TransportPolicy(retry=None)` | `ImportError` for `NO_RETRY` |
| `rate=UNLIMITED` | leave `rate` out, or `rate=None` | `ImportError` for `UNLIMITED` |
| `HttpOptions(timeout=t)` | `TransportPolicy(timeout=t)` | `TypeError` (unknown field) |
| `HttpOptions(observers=o, middleware=m)` | `AsyncBaseClient(..., observers=o, middleware=m)` | `TypeError` (unknown field) |
| `http=otel.install(HttpOptions(...))` | `observers=(otel,), middleware=(otel,)` | `AttributeError` |
| `TransportPolicy(rate_limits=(RateRule(f, r),))` | `TransportPolicy(routes=(Route(f, rate=r),))`, merged into the `Route` for `f` if there is one | `TypeError` (unknown field), `ImportError` for `RateRule` |
| a `Route` and a `RateRule` with different fragments, both matching one path | one `Route` per path, which sets everything it needs | **behaves differently, silently** (see below) |
| `Route(f, retry=None)` or `Route(f, deadline=None)`, meaning "the policy's" | leave the option out | **sends once, or has no deadline, silently** |
| `Route(f, retry=False)` | `Route(f, retry=None)` | `TypeError` naming `retry=None` |
| `SecurityOptions(allow_private_hosts=False)` | `SecurityOptions(block_private_hosts=True)` | `TypeError` (unknown field) |

Three meanings change without an error, so the CHANGELOG puts each first under **Changed**, in bold:
- **A route's explicit `retry=None` or `deadline=None`.** Each meant "keep the policy's" and now means "off", as a call's does since phase 3. Find them with `grep -rnE "Route\([^)]*(retry|deadline)=None"`.
- **Routes no longer combine with rate rules.** A path that one route and a separate rate rule both matched used to take its retry and deadline from the route and its budget from the rule. Now it takes everything from its first matching route, and what that route leaves out comes from the policy and the client's budget. The fix is one route per path that sets everything: the rate rule's `rate=` moves into the route. This one is not silent for anyone it affects: `rate_limits=` raises `TypeError`, so every such configuration has to be rewritten, and the CHANGELOG entry says so and spells out the merge.

## Decisions this plan makes

The design leaves these to the builder, or does not settle them. Each is the planner's call, made by the maintainer's rule: the cleanest design, even where it breaks things.

- **`Route(cache=None)`, not the design's `cache=False`.** 4.2 makes `None` the one spelling of off, and `TransportPolicy(cache=None)` already means "no cache". So a route turns the cache off for its path the same way. `RouteOptions.cache` is typed `None`: off is the only thing a route can say about the cache, and leaving the key out keeps the client's cache. A per-route `HttpCache` would need a cache layer per route, which nobody has asked for. The design's 4.3 is amended in Task 1.
- **`Route` is a final class, not a dataclass.** A dataclass cannot take `**options`, and the options are a closed `TypedDict`, so "left out" is real absence, as in `CallOptions`.
  - `Route.options` is a read-only mapping of what the route set.
  - `__eq__` and `__hash__` compare the fragment and the options. `TransportPolicy` is frozen and compares its routes; a shared `RateBudget` compares by identity.
  - The `repr` is `Route('/exports', rate=Rate(...))`.
  - The runtime refuses an unknown option, as phase 3's `check_options` does, so `Route(f, rates=...)` raises `TypeError`.
- **One function picks a call's route, for every layer.**
  - `_prepare` matches the route once, from the request URL, and puts it on the call's context as a `Slot[Route]`.
  - The retry and deadline code reads it from there, as today.
  - The rate layer reads it too. `PathBudgets` holds one budget per route that sets `rate`, built at construction: a `Rate` becomes a budget of its own, a `RateBudget` is used as it is, and `rate=None` gets a private budget that paces nothing but still takes the server's pauses on that path. A route that leaves `rate` out uses the client's budget.
  - The cache layer reads it, and a route with `cache=None` is neither looked up nor stored. `HttpCache(paths=)` stays as the cache's own filter.
  - Each later hop of a redirect keeps the route the call was given. Today a hop to another path can pick another rate rule. That is the "one route, whole" rule applied to a call's hops. Each page of a walk is a call of its own, so it is matched by its own URL, as today.
  - A request no client call built (a token mint) has no route on its context. It falls back to `match_route` on its own URL, so `headgate.testing.budget_for(client, url)` still answers per URL.
- **`rate=None` is the default**, and the client builds a private unpaced budget for it from a private constant. The budget still waits out a 429 or 503 `Retry-After` and a spent `RateLimit` quota, as the `UNLIMITED` default does today. `policy_disabled()` uses the same constant.
- **`TransportPolicy(retry=None)` holds `None`.** The stack reads `None` as "send once" where it resolves a call's policy, from the same private constant phase 3's call-level `None` uses. `_effective_policy(off=True)` sets `retry=None`.
- **`timeout` lives on `TransportPolicy`, and `TransportPolicy` checks it against its own `deadline`.** `check_timeout_fits(timeout, deadline)` runs in `__post_init__`, so a timeout longer than the deadline fails where both are written, not in the client constructor.
- **`OpenTelemetryObserver.install()` is removed.** It existed to add one object to two `HttpOptions` fields. The constructor now takes both directly, so `observers=(otel,), middleware=(otel,)` says the same thing, and a helper that returned a modified `HttpOptions` no longer has a field to modify. The observability guide shows the two keywords and says why the observer is in both.
- **No template matching.** The design leaves `Route(t"orders/{id}")` to the builder. This phase leaves it out. L3 is changing what a route matches against in the same lines, and matching by template is additive, so it can follow on the new shape without breaking anything.
- **The kind checks finish M22.** Phase 3 covered a call's and a view's `retry`, `follow_redirects`, `cache_ttl`, `max_response_bytes` and `body`. This phase adds a check for every field of `TransportPolicy`, `HttpOptions`, `SecurityOptions`, `IdempotencyKeys` and `Route`, and for every constructor argument. Each goes through `check_kind`, with the project's message style: the option, what it takes, what it got. The checks are:
  - **The ones M22 names:**
    - `Route(f, retry=False)` (today it retries silently);
    - `TransportPolicy(idempotency=True)`;
    - `TransportPolicy(routes=("/x",))`, where each entry must be a `Route`;
    - `HttpOptions(transport=httpx2.HTTPTransport())`, a sync transport, where `httpx2.AsyncBaseTransport` is required;
    - `HttpOptions(request_id_headers="x-request-id")`, a bare `str`, which today is split into characters. A `str` given where a collection of names goes is refused, and `security.trusted_origins` gets the same check.
  - **The rest, with no reported failure yet:**
    - `TransportPolicy.cache` is an `HttpCache` or `None`;
    - `deadline` is a number or `None` (bools refused);
    - `HttpOptions.headers` is a mapping, `limits` an `httpx2.Limits`, `ssl_context` an `ssl.SSLContext` or `None`, and `security` a `SecurityOptions`;
    - `follow_redirects`, `cookies` and `block_private_hosts` are `bool`s;
    - `max_redirects` is an `int` (bools refused);
    - `IdempotencyKeys.factory` is callable;
    - `RouteOptions` values are of their declared kinds;
    - `observers` and `middleware` are sequences, not a bare object. A bare `str` is refused there too.

## Review Focus

These six cases are not covered by the design's own examples and are the most likely to bite users. Each line gets a test in the task named.

1. **One route, whole, at every layer.** A path with `Route("/exports", rate=Rate.per_second(1))` and a policy retry of 3 attempts:
   - spends from the route's budget and retries 3 times;
   - a second route `Route("/exports/big", retry=None)` listed first takes the path whole, so it sends once and spends from the client's budget;
   - a redirect from `/exports/a` to `/other` keeps the call's route on the second hop;
   - a redirect to another origin keeps the call's route and spends that hop's turn from the route's budget, but M11's own-origin filter still keeps that hop's 429 `Retry-After` from pausing the budget.

   Covered in Task 4.
2. **The silent changes.**
   - `Route(f, retry=None)` sends once.
   - `Route(f, deadline=None)` has no deadline under `TransportPolicy(deadline=1)`, checked with `TestClock`.
   - `Route(f)` leaves both to the policy.
   - `Route(f, rate=None)` paces nothing on its path, yet still waits out a 429 `Retry-After` there.

   Covered in Task 4.
3. **`Route(cache=None)`.**
   - A matching GET on a cached client is neither served from the cache nor stored: two calls reach the network twice.
   - A route that leaves `cache` out caches as before.

   Covered in Task 4.
4. **"Off" at the policy level.**
   - `TransportPolicy(retry=None)` sends once.
   - A call's `retry=RetryPolicy(...)` still wins over it.
   - `rate=None` and no `rate` at all both pace nothing, yet still wait out a 429.
   - `policy_disabled()` still forces both off.

   Covered in Tasks 2 and 3.
5. **The moves fail loudly.**
   - `HttpOptions(timeout=...)`, `HttpOptions(observers=...)`, `HttpOptions(middleware=...)`, `TransportPolicy(rate_limits=...)` and `SecurityOptions(allow_private_hosts=...)` each raise `TypeError`.
   - `NO_RETRY`, `UNLIMITED` and `RateRule` are gone from `headgate`, `headgate.retry` and `headgate.ratelimit`, checked with `hasattr`.
   - A timeout longer than the deadline fails in `TransportPolicy(...)`.

   Covered in Tasks 2, 3 and 5.
6. **Every kind check in Decisions** raises `TypeError` with the exact message, at construction, with no request sent. Each is one parametrized case. Covered in Task 6.

---

### Task 1: Branch, and commit this plan

**Files:**
- Create: `specs/2026-11-14-phase-4-configuration-groups.md` (this plan).
- Modify: `specs/2026-11-05-public-api-redesign-design.md` 4.3: `Route(cache=None)`; template matching left to a later phase. Mark it "(Amended with the phase 4 plan.)".

- [ ] **Step 1:** Work in `claude/api-redesign-phase-4`, cut from `origin/main` at `ae4f551`.
- [ ] **Step 2:** Run `uv run typos specs/`. Expected: no output.
- [ ] **Step 3:** Commit: `Add the phase 4 plan of the API redesign`. Open it as a draft PR for review. `src/` stays untouched until the plan is approved.

### Task 2: `timeout` on `TransportPolicy`; `observers` and `middleware` on the constructor

**Files:**
- Modify: `src/headgate/_options.py`:
  - `HttpOptions` loses `timeout`, `observers` and `middleware`, and their checks move with them.
  - `TransportPolicy` gains `timeout` and runs `check_timeout_fits` in `__post_init__`.
- Modify: `src/headgate/_client.py`:
  - `__init__` takes `observers` and `middleware`, kind-checked as sequences of their protocols.
  - The emitter, `self._timeout` and `check_timeout_fits` move to read the policy.
- Modify: `src/headgate/_stack.py`: `build_transport_stack` and `Credentials.of` take the observers and middleware as arguments, not from `http`. The credential guard still sees every middleware's declared credentials.
- Modify: `src/headgate/otel.py`: remove `install`, and update the module docstring's example.
- Modify: every caller, found by `uv run ty check` and by `grep -rnE "HttpOptions\([^)]*(timeout|observers|middleware)=|\.install\(" src tests docs examples README.md`. Docs: `docs/configuration.md`, `docs/observability.md`, `docs/deadlines.md` and `docs/testing.md`.
- Test: `tests/client/test_options.py`, `tests/client/test_timeouts_fit_deadline.py`, `tests/observe/`, `tests/client/test_middleware.py`.

- [ ] **Step 1: Write the failing tests:**
  - `TransportPolicy(timeout=Timeouts(read=120.0), deadline=60.0)` raises `ValueError`, with today's message, at construction.
  - A client built with `observers=(recorder,)` emits `CallStarted`.
  - A client built with `middleware=(stamper,)` stamps every attempt, and the credential guard still withholds the middleware's declared header from a foreign origin.
  - Review Focus 5's three `HttpOptions` keywords raise `TypeError`.
- [ ] **Step 2:** Run them. Expected: FAIL.
- [ ] **Step 3:** Implement as in **Files**.
- [ ] **Step 4:** Run every gate and update the API snapshot.
- [ ] **Step 5:** CHANGELOG under **Changed**: the three moves, each with what to write instead. Under **Removed**: `OpenTelemetryObserver.install()`, with `observers=(otel,), middleware=(otel,)`. Commit: `Group timeouts with the deadline, and observers and middleware with the client`.

### Task 3: `None` is off in `TransportPolicy` and `rate=`

**Files:**
- Modify: `src/headgate/_options.py`: `TransportPolicy.retry: RetryPolicy | None`; the docstring and the kind check's message.
- Modify: `src/headgate/retry/_policy.py` and `retry/__init__.py`: `NO_RETRY` becomes private (`SEND_ONCE`), and is dropped from `__all__` and the module docstring.
- Modify: `src/headgate/ratelimit/_rate.py` and `ratelimit/__init__.py`: `UNLIMITED` becomes private (`UNPACED`), and is dropped from `__all__` and the module docstring.
- Modify: `src/headgate/_client.py`:
  - `rate: Rate | RateBudget | None = None`, whose kind check accepts `None`;
  - `_retry_of`, which reads the policy's `None` as send-once;
  - `_effective_policy`.
- Modify: `src/headgate/_view.py`: the private constant.
- Modify: `src/headgate/ratelimit/_budgets.py`: `as_budget(None)` builds the unpaced budget.
- Modify: `src/headgate/policy.py`'s docstring of what "off" means.
- Modify: every caller (`grep -rn "NO_RETRY\|UNLIMITED" src tests docs examples README.md`), including the spelling in `tests/retry/test_properties.py`. Docs: `docs/retries.md`, `docs/rate-budgets.md`, `docs/configuration.md`, `README.md`.
- Test: `tests/client/test_policy.py`, `tests/client/test_retry_opt_out.py`, and `tests/ratelimit/`.

- [ ] **Step 1: Write the failing tests:** the four parts of Review Focus 4. Add the `hasattr` checks for `NO_RETRY` and `UNLIMITED` from Review Focus 5.
- [ ] **Step 2:** Run them. Expected: FAIL.
- [ ] **Step 3:** Implement as in **Files**.
- [ ] **Step 4:** Run every gate and update the snapshot.
- [ ] **Step 5:** CHANGELOG under **Changed**: `retry=NO_RETRY` → `retry=None`, and `rate=UNLIMITED` → leave it out. Under **Removed**: `NO_RETRY` and `UNLIMITED`. Commit: `Make None the one spelling of off in TransportPolicy and rate=`.

### Task 4: One per-path rule

**Files:**
- Modify: `src/headgate/_options.py`:
  - `RouteOptions` (closed `TypedDict`) and `Route` as in **Public signatures** and **Decisions**;
  - `TransportPolicy` loses `rate_limits`;
  - `match_route` stays the one matching function. Its form is the URL one once L3 lands.
- Modify: `src/headgate/ratelimit/_rate.py`, `ratelimit/__init__.py` and `src/headgate/__init__.py`: remove `RateRule`, and export `RouteOptions` beside `Route`.
- Modify: `src/headgate/ratelimit/_budgets.py`:
  - `PathBudgets(default=, routes=, timing=, origin=)` builds one budget per route that sets `rate`, keeping M11's `origin=`;
  - `for_request` reads the call's route from its context, falling back to `match_route` for a request no call built.
- Modify: `src/headgate/_client.py`:
  - `_prepare` matches once and puts the route on the context;
  - `_retry_of` and `_deadline_of` read the route's options by presence: an option left out inherits, and `None` is off;
  - `__init__` builds `PathBudgets` from `policy.routes`.
- Modify: `src/headgate/cache/_transport.py`: a request whose route sets `cache=None` goes straight to the layer below, like a request `HttpCache` does not name.
- Modify: `src/headgate/testing/_inspect.py`: `budget_for`'s docstring names routes.
- Modify: `src/headgate/policy.py`, `docs/rate-budgets.md`, `docs/configuration.md`, `docs/retries.md`, `docs/deadlines.md`, `docs/caching.md`, `docs/testing.md`, `docs/design.md`, `README.md` and `examples/orders_client.py`: every `RateRule` and `rate_limits` becomes a `Route`.
- Test: `tests/client/test_routes.py` (new), `tests/ratelimit/`, `tests/cache/test_rules.py` and `tests/testing/`.

- [ ] **Step 1: Write the failing tests:**
  - Review Focus 1, 2 and 3.
  - `Route` equality and hashing: two routes with the same fragment and options are equal; a different shared budget makes them unequal.
  - The `repr`.
  - `Route(f, rates=1)` raises `TypeError` naming `rates`.
  - `Route(f, retry=False)` raises `TypeError` saying to write `retry=None`.
- [ ] **Step 2:** Run them. Expected: FAIL.
- [ ] **Step 3:** Implement as in **Files**.
- [ ] **Step 4:** Run every gate, the benchmarks, and update the snapshot.
- [ ] **Step 5:** CHANGELOG. Under **Changed**: the two silent rows first, in bold, with the grep and the merge recipe; then `Route` taking `rate=` and `cache=`, and a call's hops keeping its route. Under **Removed**: `RateRule` and `rate_limits`. Under **Added**: `RouteOptions`. Commit: `Make Route the one per-path rule`.

### Task 5: `block_private_hosts`

**Files:**
- Modify: `src/headgate/_options.py`: `SecurityOptions.block_private_hosts: bool = False`, its docstring, the resolver check and the proxy check's messages.
- Modify: every reader and docstring (`grep -rn allow_private_hosts src tests docs examples README.md`), among them `_stack.py`, `_curl.py`, `security/`, `errors.py`, `_client.py` and `docs/security.md`.
- Test: `tests/security/`.

- [ ] **Step 1: Write the failing tests:**
  - `SecurityOptions(block_private_hosts=True)` refuses a request to `127.0.0.1` with `ForbiddenDestinationError`.
  - The default allows it.
  - `resolver=` without `block_private_hosts=True` raises `ValueError` naming `block_private_hosts=True`.
  - `allow_private_hosts=` raises `TypeError`.
- [ ] **Step 2:** Run them. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run every gate and update the snapshot.
- [ ] **Step 5:** CHANGELOG under **Changed**. Commit: `Name the SSRF guard's switch block_private_hosts, removing a double negative`.

### Task 6: Finish the construction checks (M22)

**Files:**
- Modify: `src/headgate/_options.py`, `src/headgate/_client.py` (the constructor) and `src/headgate/_core/_kinds.py`. If a check for "a collection of names, not a `str`" is needed, it joins `check_kind` there, so every module words it the same way.
- Test: `tests/client/test_options.py`: one parametrized test, `test_every_option_is_checked_for_its_kind`, with one case per check in **Decisions**.

- [ ] **Step 1: Write the failing test.** Each case raises `TypeError` with its exact message at construction. The M22 cases also assert that nothing was sent. Today some of them fail later, at the call.
- [ ] **Step 2:** Run it. Expected: FAIL, for every case not yet checked.
- [ ] **Step 3:** Add the checks.
- [ ] **Step 4:** Run every gate.
- [ ] **Step 5:** CHANGELOG under **Fixed**: the M22 cases, each saying what used to happen, such as `Route(retry=False)` retrying or `request_id_headers="x-request-id"` being split into characters. Commit: `Check every configuration value's kind where it is written`.

### Task 7: Verify the phase and open its PR

- [ ] **Step 1:** Run every gate from a clean tree, and the docs build (`uv run --locked --only-group docs zensical build --strict --clean`).
- [ ] **Step 2:** Run `grep -rnE "NO_RETRY|UNLIMITED|RateRule|rate_limits|allow_private_hosts|HttpOptions\([^)]*(timeout|observers|middleware)=|\.install\(" src tests docs examples README.md`. Expected: matches only in CHANGELOG entries.
- [ ] **Step 3:** Compare the benchmarks against `origin/main`.
- [ ] **Step 4:** Dispatch the whole-branch review. Fix what it finds, as separate commits.
- [ ] **Step 5:** Push and open the PR, titled `Phase 4 of the API redesign: configuration grouped by what it is for`. The body lists the Migration table and links the design's section 4. Do not merge.
