# Phase 5: Seams, naming, shared state, and `Client` by composition — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every seam an extension needs is public and typed. Live shared state is always an object the caller constructs and passes, never hidden. A client is something an SDK holds, not inherits from.

**Architecture:**
- **One timing seam.** `headgate.timing` exports `Timing` (clock, sleep, wall clock and random source), with its protocols `Clock`, `Sleep` and `RandomSource`. Every class that measures, waits or draws takes `timing: Timing | None = None`, and `None` reads the timing current when the object is built. So `TestClock.install()` reaches everything built in its block: clients, token auths, shared budgets and registries. This folds in review findings L16 and L35.
- **Public seam types.** `headgate.security` exports `CredentialCarrier` and `CredentialStamper`. `headgate.call_info(request)` gives middleware a read-only view of its call.
- **Live shared state.** Under one naming rule, `RetryBudget` becomes live shared state, as `RateBudget` is: a frozen value names a per-client budget, and a constructed object is shared by whoever holds it. Breakers live in a registry each client owns, unless it is handed one to share. The process-wide registry, headgate's last hidden global, goes.
- **`Client` by composition.** `AsyncBaseClient` becomes `headgate.Client`, a final class an SDK holds. `headgate.testing` gains a way to build every `Client` in a block over a test transport, so an SDK need not forward `transport=`.

**Tech Stack:** Python 3.14, httpx2 2.16, ty, ruff, pytest, syrupy (API snapshot).

**Spec:** `specs/2026-11-05-public-api-redesign-design.md`, section 5. Phases 1–4 merged as #56, #60, #85 and #94. Review findings L16 and L35, "the one timing seam for auth and budgets", are folded in, as the November codebase review's plan says.

## Global Constraints

- Python 3.14 or newer; httpx2 2.16 or newer; hishel 2.
- Base branch: Tasks 2–5 start from `origin/main` at `9379587` (#94), in parallel with Phase 6, and rebase over Phase 6 when it merges. Task 6 starts only after Phase 6 has merged. Phase 5 ships as two PRs: Tasks 2–5, then Task 6.
- Clean break: no deprecation aliases, and no old spelling kept next to the new one.
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
- Each task adds a `CHANGELOG.md` entry under `## [Unreleased]`'s existing **Added**, **Changed**, **Removed** or **Fixed** sections. Each change or removal says what to write instead.
- Docstrings describe what the code does now, in the present tense.
- The property tests in `tests/retry/test_properties.py` keep their properties and assertions. Only their spellings change (`clock=` → `timing=`, the client class).
- The verb-path benchmarks (`benchmarks/test_client.py`, `benchmarks/test_hot_paths.py`) show no regression against `origin/main`.
- Out of scope:
  - Errors (phase 6, merged first): this phase only renames what Phase 6's code calls `AsyncBaseClient` and changes the value of `source` (see **Coordination with Phase 6**).
  - Streams, pages, `query()`, `Timeouts.UNBOUNDED` (phase 7).
  - The top-level trim (phase 8): a name this phase adds is exported where its neighbors are.

## Coordination with Phase 6

Phase 6 (errors, design section 6) is coded first and merges before any of this phase's code. This plan assumes it has merged. That means:
- every `ClientError` attribute is a read-only property, `source` included;
- `ClientError` has `sent: bool`, and `ErrorDetails` carries it;
- `retryable` has one meaning, so `CircuitOpenError` and a budget pause are `retryable=True`;
- `UpstreamStatusError.status_code` is `int | None`, with `None` only for headgate's own rate budget's refusal;
- `CallContext` has a call-level `sent` flag that `SendProgress.add_sent` sets.

Phase 5 then applies over that, touching Phase 6's lines only as follows:

| File | What Phase 5 changes, over Phase 6 |
| --- | --- |
| `src/headgate/errors.py`, `docs/errors.md` | only docstrings and prose that name `AsyncBaseClient` (Task 6) |
| `src/headgate/_failures.py` | nothing: `source`'s value changes where the client sets it, in `__init__` |
| `src/headgate/_client.py` | `__init__` (registry, timing, `_source`), `_effective_policy`, `paginate`'s body, the class line and docstring, and `__init_subclass__`. The breaker and budget `except` arms, `_site` and `_unsent_site` stay as Phase 6 leaves them. |
| `src/headgate/_core/_context.py` | a new `call_info()` beside `CallContext`, reading `attempt` and `remaining()`. `sent` and `SendProgress` are untouched, and `CallView` does not expose `sent`: an error carries it, and a middleware has the response. |
| `src/headgate/breaker/_breaker.py` | `CircuitBreaker.__init__`'s `clock=` becomes `timing=`; `CircuitOpenError`'s `retryable` is Phase 6's |
| `src/headgate/retry/`, `src/headgate/ratelimit/_budget.py` | `RetryShare` and the live `RetryBudget`; `timing=` in place of `jitter=` and `wall_clock=`. Phase 6's `retryable` and `status_code=None` are not in these lines. |
| `tests/**` | Task 6's mechanical pass, `StubClient` → a held `Client`, runs over Phase 6's new tests too. The error-message snapshot's `[StubClient]` prefixes become hosts. |

**Pagination:** this phase touches `pagination/` only in `paginate`'s body in `_client.py`. Its calls to `AsyncBaseClient.get(self, ...)`, which guarded against a subclass's own `get`, become `self.get(...)`.

## Public signatures after this phase

```python
# headgate.timing (new module)
class Clock(Protocol): def __call__(self) -> float          # monotonic seconds
class Sleep(Protocol): def __call__(self, seconds: float, /) -> Awaitable[None]
class RandomSource(Protocol): def __call__(self) -> float   # a draw in [0, 1)

@dataclass(frozen=True, slots=True, kw_only=True)
class Timing:
    clock: Clock = time.monotonic
    sleep: Sleep = asyncio_sleep
    wall_clock: Clock = time.time                           # new: Unix seconds, for Retry-After dates and RateLimit epochs
    random: RandomSource = system_random                    # new: backoff jitter, moved from RetryPolicy(jitter=)

# headgate.security: exports CredentialCarrier and CredentialStamper (moved from headgate._core)

# headgate (top level)
def call_info(request: httpx2.Request) -> CallView | None
# headgate.events
@dataclass(frozen=True, slots=True)
class CallView:
    call: CallInfo            # method, url, route template, full url: the record events carry
    attempt: int              # 1 for the first send
    remaining: float | None   # seconds left before the deadline, None for none

@final
class Client:                 # was AsyncBaseClient
    def __init__(self, *, base_url, rate=None, auth=None, policy=..., http=..., observers=(), middleware=()) -> None
    # verbs, stream, paginate, with_options -> Client, aclose, async with: unchanged

# headgate.retry
@dataclass(frozen=True, slots=True, kw_only=True)
class RetryShare:             # was RetryBudget's values: ratio, min_per_second, window
    ratio: float = 0.2
    min_per_second: float = 10.0
    window: float = 10.0

@final
class RetryBudget:            # now live shared state, like RateBudget
    def __init__(self, share: RetryShare = RetryShare(), *, timing: Timing | None = None) -> None
    def __repr__(self) -> str  # shows the share and the retries and first attempts in the window

class RetryPolicy:
    budget: RetryShare | RetryBudget | None = RetryShare()   # a share: the client's own budget; a RetryBudget: shared; None: no cap
    # jitter: removed (Timing.random)

# headgate.ratelimit
class RateBudget:
    def __init__(self, rate, *, max_in_flight=None, adaptive=..., timing: Timing | None = None) -> None   # wall_clock= removed
    def __repr__(self) -> str  # new: rate, effective rate, in flight, pause remaining

# headgate.breaker
class BreakerPolicy:
    failure_threshold: int = 5
    recovery_timeout: float = 60.0
    registry: BreakerRegistry | None = None    # None: the client's own breakers; a registry: shared with whoever holds it
class BreakerRegistry:
    def __init__(self, *, max_hosts: int = DEFAULT_MAX_HOSTS, timing: Timing | None = None) -> None   # clock= removed
    def __repr__(self) -> str  # new: each origin's state
# headgate.health
def breaker_states(registry: BreakerRegistry) -> dict[str, BreakerSnapshot]   # the registry is required

# headgate.auth (CachedTokenAuth and its subclasses)
class CachedTokenAuth:
    def __init__(self, *, refresh_margin: float = 120.0, timing: Timing | None = None) -> None

# headgate.testing
@contextmanager
def fake_network(transport: httpx2.AsyncBaseTransport) -> Iterator[httpx2.AsyncBaseTransport]
```

Removed:
- `AsyncBaseClient` (now `Client`);
- `headgate.breaker.DEFAULT_REGISTRY`;
- `headgate.testing.isolated_breakers`;
- `RetryPolicy(jitter=)` and `RetryBudget(clock=)`;
- `RateBudget(wall_clock=)` and `BreakerRegistry(clock=)`;
- `headgate._core`'s seam names, which move to `headgate.timing` and `headgate.security`.

## Migration

| Before | After | The old spelling now |
| --- | --- | --- |
| `class GitHub(AsyncBaseClient): ... super().__init__(...)`, then `self.get(...)` | `class GitHub: self._http = Client(...)`, then `self._http.get(...)` | `ImportError`, and `Client` is final (`TypeError` on subclassing) |
| `RetryPolicy(budget=RetryBudget(ratio=0.1))` | `RetryPolicy(budget=RetryShare(ratio=0.1))`: still the client's own budget | **`RetryBudget(ratio=...)` raises `TypeError`**: it takes a `RetryShare` |
| one retry budget for several clients | `shared = RetryBudget(RetryShare(...))`, then `RetryPolicy(budget=shared)` in each | — (new) |
| two clients on one origin, pooling failures by default | `BreakerPolicy(registry=shared)` in each, with one `shared = BreakerRegistry()` | **each client now has its own breakers, silently** (see below) |
| `breaker_states()` | `breaker_states(registry)` | `TypeError` (missing argument) |
| `isolated_breakers()` | nothing: a client's breakers are its own | `ImportError` |
| `BreakerRegistry(clock=c)`, `RateBudget(wall_clock=w)`, `RetryPolicy(jitter=j)` | `timing=Timing(clock=c, wall_clock=w, random=j)` | `TypeError` |
| `from headgate._core import CredentialStamper` | `from headgate.security import CredentialStamper` | the private import keeps working, but is undocumented |
| `from headgate._core import CallContext` in a middleware | `headgate.call_info(request)` | as above |
| an SDK forwarding `transport=` only for its tests | `with headgate.testing.fake_network(FakeTransport(...)):` around building the SDK | — (new) |

One change happens without an error: **two clients on one origin no longer pool their breaker failures unless they are given one registry.** This is design 5.3's behavior change, the one exception to "same behavior". The CHANGELOG puts it first under **Changed**, in bold, with the two-line recipe for keeping the old pooling.

## Decisions this plan makes

The design leaves these to the builder, or does not settle them. Each is the planner's call, made by the maintainer's rule: the cleanest design, even where it breaks things.

- **`RetryShare` and a live `RetryBudget`, mirroring `Rate` and `RateBudget`.** The design (5.2) makes `RetryBudget` live and has `RetryPolicy.budget` default to `None`, meaning "the client makes its own". That collides with phase 4's rule that `None` is off: today `budget=None` means no cap. A live default object cannot work either. The module-level default `TransportPolicy()` would hand one live budget to every client that passes no policy, which is hidden sharing. So the client already takes the other side's shape, `rate: Rate | RateBudget | None`, and the retry budget takes the same:
  - a frozen value, `RetryShare(ratio, min_per_second, window)`, from which each client builds a budget of its own;
  - a live `RetryBudget(share)`, shared by every client given the same object;
  - `None`, which is no cap.

  `RetryShare` is a new name, and it replaces the old frozen `RetryBudget` one for one, so no concept is added: the value and the live object now have different names, as rate's do. The design's 5.2 is amended in Task 1.
- **`BreakerPolicy(registry=None)` means the client's own breakers.** Here `None` is off for sharing, and the breaker itself is off with `TransportPolicy(breaker=None)`, as today.
  - **Thresholds.** A registry remembers the thresholds of the first policy that uses it. A client whose `BreakerPolicy` names that registry with different thresholds raises `ValueError` when the client is built, naming both pairs. That realizes the design's "a passed registry holds one breaker per origin" with a check that can run before any origin is known.
  - **Keying.** Breakers are keyed by origin alone, no longer by origin and thresholds.
- **One `Timing`, with four seams.** `wall_clock` joins it, because a budget reads `Retry-After` dates and `RateLimit` epochs in Unix time, and `random` joins it, because backoff jitter is randomness that a test pins. `RetryPolicy(jitter=)` goes, since a policy is configuration and a random source is a seam. `Timing(...)` can be passed in each class's `timing=`. `None` there reads `headgate.timing.current_timing()` once, when the object is built, as a client does today. That is what makes `TestClock.install()` reach a token auth, a shared budget or a registry built in its block, which closes L16 and L35. `timing_override` moves to `headgate.timing` beside `current_timing`, so a test kit of the user's own can install a clock as `TestClock` does.
- **`call_info(request)` returns a snapshot, `CallView`.** The design names its return type `CallInfo`, but `CallInfo` is the record events carry, made once when a call starts. The attempt number and the time left change while the call runs. So `call_info` returns a fresh, frozen `CallView(call: CallInfo, attempt, remaining)` each time it is called, `None` for a request no client call built, and `CallView` lives in `headgate.events` beside `CallInfo`. `CallContext` stays private. The design's 5.1 is amended in Task 1.
- **`Client`'s errors name its origin, not a class.** A `ClientError`'s `source`, its `[source]` prefix, named the subclass (`[GitHub]`). A final `Client` would make every message read `[Client]`, which tells nobody anything. So the source is the `base_url`'s host (`[api.github.com]`), which still tells two SDKs apart and needs no new argument. The constructor stays at seven. It is set where the client sets `_source`, so `_failures.py` and Phase 6's read-only `source` property are untouched (see **Coordination with Phase 6**). The error-message snapshot is updated with it.
- **`fake_network(transport)` replaces the transport of every `Client` built in its block,** even one that sets `HttpOptions(transport=...)` itself. That is the point: an SDK that fixes `AsyncCurlTransport` in its constructor can still be tested over a `FakeTransport`. The override replaces only the network transport, so the stack above it (rate, cache, retry, breaker) is the SDK's own. It nests with `TestClock.install()`, `instant_retries()` and `policy_disabled()`, like them a construction-time switch, and yields the transport. It is in `headgate.testing`, which is where the design says the builder picks its shape.
- **The live objects show their state.** `RateBudget`, `RetryBudget` and `BreakerRegistry` gain a `repr` that shows their state, as 5.2 asks:
  - `RateBudget(rate=Rate(10/s), effective=8.0/s, in_flight=2, paused=0.0s)`;
  - `RetryBudget(share=RetryShare(...), retries=3, firsts=40)`;
  - `BreakerRegistry({'https://api.example': 'closed'})`.
  The exact formats are up to the implementer, but no `repr` may include a credential.
- **`Client` is `@final`, and subclassing it raises `TypeError`** in `__init_subclass__`, naming what to write instead (hold a `Client`). `@final` alone stops only a type checker, and a runtime check is cheap.

## Review Focus

These six cases are not covered by the design's own examples and are the most likely to bite users. Each line gets a test in the task named.

1. **`TestClock.install()` reaches everything built in its block:**
   - a `CachedTokenAuth`'s token expires and is refreshed on `clock.advance(...)`;
   - a shared `RateBudget` paces on the test clock;
   - a `BreakerRegistry`'s cooldown runs on it;
   - a `RetryBudget`'s window slides on it.

   - a `RateBudget` shared by two clients, built in the block, paces both on the test clock.

   An object built outside the block keeps the system clock. Covered in Task 2.
2. **Retry budgets:**
   - Two clients given one `RetryBudget` spend one window: the second client's retries are refused once the first has spent the budget.
   - Two clients given one `RetryShare`, or none, each keep their own.
   - One client's routes and views share that client's budget.

   Covered in Task 4.
3. **Breakers:**
   - Two clients on one origin, given no registry, do not pool: one client's failures leave the other's breaker closed.
   - Two given one registry do pool.
   - A client naming a registry with other thresholds raises `ValueError` when built.
   - `breaker_states(registry)` reports what the registry holds.

   Covered in Task 5.
4. **`call_info` inside a middleware** sees the route template of a t-string call, `attempt` 1 and then 2 on a retry, and `remaining` falling under a deadline. It returns `None` for a request built by hand. Covered in Task 3.
5. **`fake_network`:**
   - An SDK whose constructor sets `HttpOptions(transport=AsyncCurlTransport(...))` sends to the fake inside the block, and to its own transport outside it.
   - The SDK's rate budget and retries still run over the fake.

   Covered in Task 6.
6. **`Client` is held, not inherited:**
   - `class X(Client)` raises `TypeError` with the message.
   - `with_options` returns a `Client`.
   - An error from `Client(base_url="https://api.github.com")` reads `[api.github.com] ...`.

   Covered in Task 6.

---

### Task 1: Branch, and commit this plan

**Files:**
- Create: `specs/2026-11-14-phase-5-seams-and-client.md` (this plan).
- Modify: `specs/2026-11-05-public-api-redesign-design.md`:
  - 5.1: `call_info` returns a `CallView`; `Timing` has four seams.
  - 5.2: `RetryShare` and a live `RetryBudget`, with `budget=None` meaning no cap.
  - 5.3: a registry fixes its thresholds when first used.
  - 5.4: the source is the origin's host; `fake_network`.

  Mark each "(Amended with the phase 5 plan.)".

- [ ] **Step 1:** Work in `claude/api-redesign-phase-5`, cut from `origin/main` at `9379587`.
- [ ] **Step 2:** Run `uv run typos specs/`. Expected: no output.
- [ ] **Step 3:** Commit: `Add the phase 5 plan of the API redesign`. Open it as a draft PR for review. `src/` stays untouched until the plan is approved.

### Task 2: One timing seam (L16, L35)

**Files:**
- Create: `src/headgate/timing.py`, exporting `Timing`, `Clock`, `Sleep`, `RandomSource`, `current_timing` and `timing_override`. `headgate._core._time` keeps the implementation and re-exports for the private layers.
- Modify: `src/headgate/_core/_time.py`: `Timing` gains `wall_clock` and `random`.
- Modify: `src/headgate/_auth.py`:
  - `CachedTokenAuth(timing=)`;
  - every `time.monotonic()` reads `self._timing.clock()`.
- Modify: `src/headgate/ratelimit/_budget.py` and `_budgets.py`:
  - `RateBudget(timing=None)`, resolved at construction;
  - `wall_clock=` goes, replaced by `timing.wall_clock`;
  - `Pacer` reads the same.
- Modify: `src/headgate/breaker/_registry.py` and `_breaker.py`: `BreakerRegistry(timing=)` and `CircuitBreaker(timing=)`, `clock=` gone.
- Modify: `src/headgate/retry/_policy.py` and `_transport.py`: `jitter=` goes, and the retry layer draws from `timing.random`.
- Modify: `src/headgate/testing/_time.py`: `TestClock.timing` sets all four seams. Its `wall_clock` is a fixed epoch plus the test clock's reading, so a `Retry-After` date and the monotonic clock agree.
- Modify: `src/headgate/__init__.py` and the docs: the module docstring, `docs/testing.md` and `docs/design.md`.
- Test: `tests/core/test_time.py`, `tests/testing/test_clock.py` (Review Focus 1), and `tests/auth/test_cached_token_auth.py`, which today patches privates to expire a token. Rewrite it on `TestClock` and remove the patching.

- [ ] **Step 1: Write the failing tests:**
  - Each part of Review Focus 1.
  - `RetryPolicy(jitter=...)`, `RateBudget(wall_clock=...)` and `BreakerRegistry(clock=...)` raise `TypeError`.
  - `headgate.timing.Timing` is `headgate._core.Timing`.
- [ ] **Step 2:** Run them. Expected: FAIL.
- [ ] **Step 3:** Implement as in **Files**. Grep `time\.monotonic\(\)|time\.time\(\)|system_random` across `src/`. Expected: only the defaults in `_core/_time.py`.
- [ ] **Step 4:** Run every gate and update the snapshot.
- [ ] **Step 5:** CHANGELOG:
  - **Added:** `headgate.timing`.
  - **Changed:** each removed `clock=`, `wall_clock=` and `jitter=`, with its `timing=` spelling.
  - **Fixed:** `TestClock.install()` reaches token lifetimes, shared budgets and registries (L16, L35).

  Commit: `Give every clock, sleep and random draw one seam, headgate.timing`.

### Task 3: Public seam types and `call_info`

**Files:**
- Modify: `src/headgate/security/__init__.py`: export `CredentialCarrier` and `CredentialStamper` (defined in `_core/_seams.py`, unchanged).
- Modify: every docstring and message that names `headgate._core.CredentialStamper`, among them `cache/_policy.py`'s `ClientConfigurationError` and `_auth.py`. Find them with `grep -rn "_core.Credential\|_core\.CallContext" src docs`.
- Create: `call_info` in `src/headgate/_core/_context.py`, which reads the request's `CallContext`. Export it from `headgate`, and `CallView` from `headgate.events`.
- Modify: `docs/configuration.md#middleware`: a middleware reads `call_info(request)`.
- Test: `tests/client/test_middleware.py` (Review Focus 4), `tests/security/test_exports.py` (new).

- [ ] **Step 1: Write the failing tests:**
  - Review Focus 4.
  - `from headgate.security import CredentialStamper` works.
  - A cache misconfiguration's message names `headgate.security.CredentialStamper`.
- [ ] **Step 2:** Run them. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run every gate and update the snapshot.
- [ ] **Step 5:** CHANGELOG under **Added**: `call_info`, `CallView`, and the two seam exports. Commit: `Publish the seams a middleware or an auth is written against`.

### Task 4: `RetryShare`, and `RetryBudget` as live shared state

**Files:**
- Modify: `src/headgate/retry/_policy.py`:
  - the frozen dataclass `RetryBudget` becomes `RetryShare`;
  - `RetryPolicy.budget: RetryShare | RetryBudget | None = RetryShare()`;
  - its kind check refuses anything else.
- Modify: `src/headgate/retry/_budget.py`: `RetryLedger` becomes the live `RetryBudget(share, *, timing=None)`, with its `repr`.
- Modify: `src/headgate/retry/_transport.py`:
  - `_ledgers` keeps one budget per distinct `RetryShare` for the client, as today per value;
  - a passed `RetryBudget` is used as it is, shared.
- Modify: `src/headgate/retry/__init__.py`, the docs (`docs/retries.md`), `README.md` and `examples/`.
- Test: `tests/retry/test_budget_and_classify.py`, `tests/integration/test_views.py` (Review Focus 2), and `tests/retry/test_properties.py`, re-spelled only.

- [ ] **Step 1: Write the failing tests:**
  - Review Focus 2.
  - `RetryBudget(ratio=0.1)` raises `TypeError` naming `RetryShare`.
  - The `repr`.
- [ ] **Step 2:** Run them. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run every gate and the hot-path benchmarks, and update the snapshot.
- [ ] **Step 5:** CHANGELOG:
  - **Changed:** `RetryBudget(...)` as a value is `RetryShare(...)`; a `RetryBudget` object is shared by whoever holds it.
  - **Added:** `RetryShare`.

  Commit: `Make RetryBudget live shared state, named apart from the RetryShare it is built from`.

### Task 5: A client's breakers are its own

**Files:**
- Modify: `src/headgate/breaker/_registry.py`:
  - remove `DEFAULT_REGISTRY`;
  - key by origin;
  - remember the thresholds of the first policy, and refuse another with `ValueError`;
  - the `repr`.
- Modify: `src/headgate/breaker/_policy.py`: `registry: BreakerRegistry | None = None`.
- Delete: `src/headgate/breaker/_scope.py` (`registry_override` and `scoped_registry`).
- Modify: `src/headgate/_client.py`:
  - `__init__` builds a registry of its own when the policy names none, on the client's timing;
  - `_effective_policy` loses its registry substitution.
- Modify: `src/headgate/health.py`: `breaker_states(registry)`.
- Delete: `src/headgate/testing/_breakers.py`, and remove `isolated_breakers` from `headgate.testing`. Every test that entered it drops the `with`, and a test that inspected the yielded registry builds one and passes it.
- Modify: `docs/breaker.md`, `docs/testing.md`, `README.md` and `examples/`. `docs/breaker.md` quotes the `ValueError` a client raises for thresholds that differ from its shared registry's.
- Test: `tests/breaker/test_breaker_keying.py`, `tests/breaker/test_registry.py`, `tests/transport/test_health.py` and `tests/testing/test_breakers.py` (Review Focus 3); the last loses its `isolated_breakers` cases.

- [ ] **Step 1: Write the failing tests:** Review Focus 3, and that `isolated_breakers` and `DEFAULT_REGISTRY` are gone (`hasattr`).
- [ ] **Step 2:** Run them. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run every gate and update the snapshot.
- [ ] **Step 5:** CHANGELOG:
  - **Changed:** the silent row first, in bold, with the recipe; then `breaker_states(registry)`.
  - **Removed:** `DEFAULT_REGISTRY` and `isolated_breakers`.

  Commit: `Keep each client's breakers its own, shared only through a registry passed to it`.

### Task 6: `Client`, held rather than inherited

Do this task last, so its mechanical pass over `tests/` runs once, over everything Phase 6 added.

**Files:**
- Modify: `src/headgate/_client.py`:
  - `AsyncBaseClient` becomes `@final class Client`, refusing subclassing in `__init_subclass__`;
  - `_BUILT` and `vars(self)` become a plain attribute;
  - `self._source` becomes the `base_url`'s host;
  - `paginate` calls `self.get` and `self.post`;
  - the class docstring.
- Modify: `src/headgate/__init__.py` and every module that names `AsyncBaseClient`. Find them with `grep -rn AsyncBaseClient src docs examples README.md`.
- Create: `fake_network` in `src/headgate/testing/_transport.py`. The client reads the override when it builds its network transport (`_stack._network`).
- Modify: `tests/support/clients.py`: `StubClient` becomes a function returning a `Client`. Every `class X(AsyncBaseClient)` in `tests/` becomes a helper that builds a `Client` (16 today, found by `grep -rn "(AsyncBaseClient)" tests`). This is a mechanical pass, done after Phase 6's tests have landed.
- Modify: `README.md`, every guide and `examples/` hold a `Client`: no example subclasses it. `docs/` shows how an SDK forwards `async with` and `aclose()` in a few lines. `errors.py`'s docs for Phase 6's `source` say it is the `base_url`'s host.
- Modify: the error-message snapshot, whose `[StubClient]` prefixes become the test origins' hosts.
- Test: `tests/client/test_client.py` (Review Focus 6), and `tests/testing/test_fake_network.py` (new, Review Focus 5).

- [ ] **Step 1: Write the failing tests:** Review Focus 5 and 6. Also: `AsyncBaseClient` is not an attribute of `headgate`.
- [ ] **Step 2:** Run them. Expected: FAIL.
- [ ] **Step 3:** Implement as in **Files**. Do the source change first, then the test pass.
- [ ] **Step 4:** Run every gate, the docs build and the benchmarks, and update both snapshots.
- [ ] **Step 5:** CHANGELOG:
  - **Changed:** `AsyncBaseClient` → a held `Client`, with the before-and-after SDK, and the `[source]` prefix now naming the host.
  - **Added:** `fake_network`.

  Commit: `Make the client something an SDK holds: Client, final, and fake_network for its tests`.

### Task 7: Verify the phase and open its PR

- [ ] **Step 1:** Run every gate from a clean tree, and the docs build (`uv run --locked --only-group docs zensical build --strict --clean`).
- [ ] **Step 2:** Run `grep -rnE "AsyncBaseClient|isolated_breakers|DEFAULT_REGISTRY|wall_clock=|jitter=|_core\.(CredentialStamper|CredentialCarrier|CallContext|Timing)" src tests docs examples README.md`. Expected: matches only in CHANGELOG entries.
- [ ] **Step 3:** Compare the benchmarks against `origin/main`.
- [ ] **Step 4:** Dispatch the whole-branch review. Fix what it finds, as separate commits.
- [ ] **Step 5:** Push and open the PR, titled `Phase 5 of the API redesign: public seams, shared state you pass, and a Client you hold`. The body lists the Migration table and links the design's section 5. Do not merge.
