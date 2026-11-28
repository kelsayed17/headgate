# Second pass, phase 4: The surface is what users use — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The public surface is what users write and read: live objects hide the mechanism only the stack drives, no
hidden global repeats an event, each idea has one module, the SSRF guard is configured once, internal predicates go
private, every type a public signature uses is importable, and the fakes agree.

**Architecture:** Eight tasks, one per item of section 4 (4.1 split in two), each leaving `main`-quality green. A live
object keeps its mechanism in a private object it alone holds, reached through a private accessor, as `RateBudget`
already keeps its `Pacer` behind `pacer_of`: `RetryBudget` its `RetryLedger` (`ledger_of`), `BreakerRegistry` a new
`BreakerTable` (`table_of`). A method only the stack calls on a frozen value becomes a function of the private module
beside it, since a leading-underscore method could not be called from the next module without reaching a private
member (ruff `SLF001`, which applies to `tests/` too).

**Tech Stack:** Python 3.14, httpx2 2.16, hishel 2, ty, ruff, pytest, syrupy (API snapshot).

**Spec:** `specs/2026-11-27-api-second-pass-design.md`, section 4 (4.1–4.7), approved 2026-11-27.

## Global Constraints

- Python 3.14 or newer; httpx2 2.16 or newer; hishel 2.
- Branch: `claude/api-second-pass-4-surface`, stacked on `claude/api-second-pass-3-names`. No push and no PR.
- Clean break: no deprecation aliases, no old spelling kept next to the new one; never fix a finding by documenting it.
- Gates for every task: `uv run ruff check`, `uv run ruff format --check`, `uv run ty check`, `uv run typos`,
  `uv run python -m pytest --cov` at 100% line and branch coverage in one process, and
  `uv run zensical build --strict` once docs change. No `noqa`, `ty: ignore` or `pragma: no cover`.
- A public API change updates the snapshot (`uv run python -m pytest tests/package/test_api_surface.py --snapshot-update`), diff reviewed.
- Every exported name appears in `docs/`; every removed or privatized name leaves `docs/`, `README.md`, docstrings,
  `examples/`, `benchmarks/` and messages (`git grep` each).
- Each task adds a `CHANGELOG.md` entry under `## [Unreleased]` saying what to write instead, and updates an earlier
  Unreleased entry that names what it removes. Grep after every edit. No blank lines inside a list.
- `tests/retry/test_properties.py` keeps its properties and assertions; only spellings follow.
- Tests that use what becomes private reach it through its private path; no public alias is kept for them.
- A public module binds only its `__all__` and its public subpackages (`tests/package/test_one_home.py`).

## Decisions

1. **A live object's mechanism is a private object it holds, found by a private accessor.** `RetryLedger` takes the
   lock `RetryBudget` held and gains `clock`; `RetryBudget` holds only its ledger, found with
   `headgate.retry._policy.ledger_of(budget)`, as a `RateBudget`'s pacer is with `pacer_of`. `clock_of(budget)` goes:
   `ledger_of(budget).clock` replaces it. The retry layer spends a `RetryLedger`, keeping one of its own per share
   (built on the client's clock, with no `RetryBudget` around it), and a shared budget's through `ledger_of`.
2. **`BreakerTable` is the registry's mechanism.** It lives in `breaker/_registry.py` and holds what
   `BreakerRegistry` held: the policy, `max_hosts`, the timing, the lock and the breakers, with
   `breaker_for(origin)`, `breakers(origin=None)`, `clock`, `__len__` and `clear()`. `BreakerRegistry` holds only its
   table (`table_of(registry)`) and keeps its constructor, `policy`, `max_hosts`, `len()`, `clear()` and `repr`; it is
   no longer iterable. `clock_of(registry)` goes: `table_of(registry).clock` replaces it. A client keeps its
   registry's table, so the hot path makes no lookup per call.
3. **`CircuitBreaker` stays in `headgate.breaker._breaker`, under its name.** Not exported, its constructor and `track`
   are private by their home; `track` keeps its name, since it is the private `Breaker` protocol's method the client
   calls, and an underscore would need an accessor for nothing.
4. **`BreakerSnapshot` is `@dataclass(frozen=True, slots=True, kw_only=True)`** with `state: BreakerState`,
   `refusing: bool` and `failures: int` (consecutive failures since the last success), keyword-only like every event
   and `CallInfo`. A snapshot is no longer a tuple: unpacking and indexing go. `breaker_states` reads the table.
5. **`RetryPolicy.wait_after` and `Backoff.delay` become `wait_after(policy, retry_after, retry, draw)` and
   `backoff_delay(backoff, retry, draw)`**, functions of `headgate.retry._policy`. The retry layer, the benchmarks and
   the property tests call them there.
6. **A store that fails after the answer is reported too (4.2).** `cache_degradations()` and `CACHE_DEGRADATIONS` go.
   The live fallback already emits `CacheChecked(outcome=DEGRADED)`; a deferred write that fails or overruns the
   accepting call's deadline was only counted, so it now emits `CacheChecked(outcome=DEGRADED)` on the accepting
   call, after that call's `MISS`, or the removal would leave it reported nowhere. `CacheOutcome.DEGRADED` reads "the
   storage failed: the request went to the network uncached, or its response could not be stored".
   `observer_failures()` says it is process-wide, and why: an observer that raises cannot be reported through
   observers.
7. **`policy_disabled` stays defined in `headgate._core._switch`** (a rate budget honors it where it is built, below
   every layer) and is exported, so rehomed, by `headgate.testing`, which reports `headgate.testing` as its home as soon
   as it is imported. Its docstring takes what `headgate.policy`'s said about what "off" means. `headgate.policy`
   goes, and CI's import smoke test stops importing it.
8. **`SecurityOptions(check: AddressCheck | None = None)`**, `None` being `forbidden_reason`, accepted only with
   `block_private_hosts=True` (`ValueError`, as `resolver=`) and refused with `TypeError` when not callable.
   `AddressGuard`, `PinnedHTTPTransport` and `resolve_with_loop` stay in their private modules.
9. **A transport that pins itself holds a `GuardBinding`** (`security/_transport.py`), registered in a weak map the
   stack reads with `guard_binding(transport)`, which replaces the public `AsyncCurlTransport.bind_guard` and the
   `GuardedTransport` protocol: the stack binds a curl transport without a public method and without importing the
   optional curl module. The binding holds no reference to its transport, so the weak map frees both. Its refusals
   keep their messages.
10. **4.5's predicates become functions of their private modules:** `caches_request(cache, method, path)` and
    `stores_status(cache, status)` in `cache/_policy.py`, `response_parts(step)` in `testing/_faults.py` (both fakes
    use it). `Link` and `parse_link_header` stay in `pagination/_link.py`. The testing guide reads a cache's
    `methods`, `paths` and `statuses` instead.
11. **`CallUrl` becomes a `type` alias, as `Method` is**, and is exported beside it; `RawContent` and `UploadFiles`
    are exported beside `Raw` and `Multipart`. `RawContent`'s value names `Callable[[], AsyncIterable[bytes]]` where
    it named the private `BodyFactory`, which goes, so the alias a user reads names only what they can import.
    `UploadFiles` keeps its underscore building blocks (a file, and a file with its name, type and headers), which say
    by their names that they are not imports.
12. **`FaultyTransport.calls` is a property returning a tuple**, over a private list, as `FakeTransport.calls` is.

## Review Focus

1. **A shared registry's breakers survive the registry's `repr`, `len()` and `clear()`** while two clients use it, and
   `breaker_states` reads failures without building a breaker for an origin never called. Covered in Task 3.
2. **Two clients on one `AsyncCurlTransport` with different guards** still raise `ValueError` after `bind_guard`
   leaves the surface, and the binding does not keep the transport alive. Covered in Task 6.
3. **A `CacheChecked(DEGRADED)` for a failed deferred write** reaches an observer of the accepting call, and a write
   with no accepting call (a response no client sent) raises nothing. Covered in Task 4.
4. **`SecurityOptions(check=...)` reaches every hop**, including the curl transport's own vetting and a redirect's
   next hop, not only the first request. Covered in Task 6.
5. **`typing.get_type_hints` resolves `Raw`, `Multipart` and every `Client` verb** after `CallUrl` becomes a `type`
   alias (the one-home hint test covers it once the three are exported). Covered in Task 8.

---

### Task 1: Commit the plan

- [ ] `uv run typos specs/`, then `git add specs/2026-11-27-second-pass-phase-4-surface.md && git commit`.

### Task 2: `RetryBudget` and the retry values hide their mechanism (4.1, retry half)

**Files:** `retry/_budget.py`, `retry/_policy.py`, `retry/_transport.py`, `_client.py` (`_shared_clocks`),
`benchmarks/test_hot_paths.py`; tests under `tests/retry/` and any that call the three methods.

**Interfaces:** `ledger_of(budget: RetryBudget) -> RetryLedger`; `RetryLedger(share, clock)` with `clock`,
`record_first()`, `try_spend() -> bool`, `refund()`, `counts()`, each under its lock;
`wait_after(policy: RetryPolicy, retry_after: float | None, retry: int, draw: float) -> float`;
`backoff_delay(backoff: Backoff, retry: int, draw: float) -> float`.

- [ ] **Tests:** `test_a_retry_budget_shows_only_its_share` (`[n for n in dir(RetryBudget) if not n.startswith("_")] ==
  ["share"]`); `test_retry_values_hide_their_arithmetic` (`not hasattr(RetryPolicy, "wait_after")`,
  `not hasattr(Backoff, "delay")`). Run: fail.
- [ ] Implement Decisions 1 and 5; migrate tests to `ledger_of(...)`, `wait_after(...)` and `backoff_delay(...)`.
  Snapshot. Changelog **Removed** (`RetryBudget.record_first`, `try_spend`, `refund`; `RetryPolicy.wait_after`,
  `Backoff.delay`: nothing to write instead, since only the retry layer spends them; read `repr(budget)`).
- [ ] Commit.

### Task 3: Breakers hide their mechanism; `breaker_states` is the one reader (4.1, breaker half)

**Files:** `breaker/__init__.py`, `breaker/_registry.py`, `_client.py`, `_health.py`, `health.py`,
`testing/_inspect.py`, `benchmarks/test_hot_paths.py`; docs `breaker.md`, `observability.md`, `testing.md`,
`design.md`, `mutation-testing.md`; `tests/support/breakers.py` and every breaker test.

**Interfaces:** `table_of(registry: BreakerRegistry) -> BreakerTable`; `BreakerTable.breaker_for(origin) ->
CircuitBreaker`, `.breakers(origin=None) -> tuple[CircuitBreaker, ...]`, `.clock`;
`BreakerSnapshot(*, state, refusing, failures)`; `breaker_states(registry) -> dict[str, BreakerSnapshot]`.

- [ ] **Tests:** `test_a_registry_shows_only_its_settings`
  (public names of `BreakerRegistry` are `clear`, `max_hosts`, `policy`; `iter(registry)` is a `TypeError`);
  `test_circuit_breaker_is_not_exported` (`not hasattr(headgate.breaker, "CircuitBreaker")`);
  `test_a_snapshot_counts_failures` (two failures against one origin under `failure_threshold=3` read
  `BreakerSnapshot(state=CLOSED, refusing=False, failures=2)`; the snapshot is frozen and not a tuple);
  Review Focus 1. Run: fail.
- [ ] Implement Decisions 2, 3 and 4; tests read state with `breaker_states`, and build or drive a breaker with
  `table_of(registry).breaker_for(...)`. Snapshot. Changelog **Changed** (`BreakerSnapshot`) and **Removed**
  (`CircuitBreaker`, `breaker_for`, `breakers()`, iteration: write `breaker_states(registry)`).
- [ ] Commit.

### Task 4: No hidden global repeats an event (4.2)

**Files:** `cache/_transport.py`, `_core/_events.py` (`DEGRADED`'s doc), `_health.py`, `health.py`, `events.py` and
`_core/_events.py` docstrings naming `observer_failures`; docs `caching.md`, `observability.md`; tests
`tests/cache/test_fallback.py`, `test_stream_path.py`, `tests/ratelimit/test_client_feedback.py`,
`tests/transport/test_health.py`.

- [ ] **Tests:** `test_health_reads_no_cache_counter` (`not hasattr(headgate.health, "cache_degradations")`);
  `test_a_failed_store_is_reported_on_its_call` (a miss whose deferred write raises a `CacheStorageError`: an
  observer records `[MISS, DEGRADED]` for the call); Review Focus 3. Tests that read the counter assert the event or
  `CACHE_DEGRADED_EXT` instead. Run: fail.
- [ ] Implement Decision 6. Snapshot. Changelog **Removed** (`cache_degradations()`: count
  `CacheChecked(outcome=CacheOutcome.DEGRADED)` in an observer) and **Changed** (a failed deferred write is reported).
- [ ] Commit.

### Task 5: One module per idea (4.3)

**Files:** delete `policy.py`; `testing/__init__.py`, `_core/_switch.py`, `ratelimit/_budgets.py` docstrings;
`tests/package/test_api_surface.py`, `test_import_isolation.py`; `.github/workflows/ci.yml`; docs `testing.md`,
`rate-budgets.md`, `configuration.md`; every test importing it.

- [ ] **Tests:** `test_policy_disabled_lives_in_testing` (in a fresh process, `import headgate.testing` then
  `headgate.testing.policy_disabled.__module__ == "headgate.testing"`; `import headgate.policy` raises
  `ModuleNotFoundError`). Run: fail.
- [ ] Implement Decision 7. Snapshot. Changelog **Changed** (write `from headgate.testing import policy_disabled`),
  and update the Unreleased entry that names `policy_disabled()`.
- [ ] Commit.

### Task 6: The SSRF guard is configured once (4.4)

**Files:** `_options.py` (`SecurityOptions.check`), `_stack.py`, `security/__init__.py`, `security/_transport.py`,
`_curl.py`; docs `security.md`, `mutation-testing.md`; tests under `tests/security/` and the curl tests.

**Interfaces:** `SecurityOptions(*, trusted_origins, block_private_hosts, resolver: Resolver | None = None,
check: AddressCheck | None = None)`; `GuardBinding.bind(guard: AddressGuard | None) -> None`;
`guard_binding(transport: httpx2.AsyncBaseTransport) -> GuardBinding | None`.

- [ ] **Tests:** `test_a_check_widens_the_guard` (a guard with `check=lambda address: None` reaches a loopback
  `FakeTransport` host through `SecurityOptions`; the default refuses it); `test_check_needs_the_guard`
  (`SecurityOptions(check=forbidden_reason)` is a `ValueError`; `check=1` with the guard on a `TypeError`);
  `test_the_guard_pieces_are_private` (`headgate.security.__all__` holds no `AddressGuard`, `PinnedHTTPTransport` or
  `resolve_with_loop`; `AsyncCurlTransport` has no `bind_guard`); Review Focus 2 and 4. Run: fail.
- [ ] Implement Decisions 8 and 9. Snapshot. Changelog **Added** (`check=`) and **Removed** (the three names and
  `bind_guard`: write `SecurityOptions(block_private_hosts=True, check=...)`).
- [ ] Commit.

### Task 7: Internal predicates and helpers go private (4.5)

**Files:** `cache/_policy.py`, `cache/_transport.py`, `pagination/__init__.py`, `_deprecation.py` (doc),
`testing/_faults.py`, `testing/_transport.py`, `testing/_inspect.py` (doc); docs `caching.md`, `testing.md`,
`mutation-testing.md`; tests.

- [ ] **Tests:** `test_internal_predicates_are_private` (`HttpCache` has no `caches_request` or `stores_status`,
  `headgate.pagination.__all__` no `Link` or `parse_link_header`, `Respond` no `response`). Run: fail.
- [ ] Implement Decision 10. Snapshot. Changelog **Removed** (read `cache.methods`, `.paths` and `.statuses`;
  `LinkHeader` for `Link` headers).
- [ ] Commit.

### Task 8: Types in public signatures are public; the fakes agree (4.6, 4.7)

**Files:** `_url.py`, `_request_body.py`, `__init__.py`, `testing/_faults.py`; docs `configuration.md` or
`index.md` (`CallUrl`), `responses.md` or wherever `Raw` and `Multipart` are documented, `testing.md`; tests.

- [ ] **Tests:** `test_types_in_public_signatures_are_exported` (`headgate.CallUrl`, `headgate.RawContent`,
  `headgate.UploadFiles` are `TypeAliasType`s, and `RawContent`'s value names no `BodyFactory`);
  `test_faulty_transport_calls_is_a_tuple` (`type(network.calls) is tuple`, and assigning it raises
  `AttributeError`); Review Focus 5. Run: fail.
- [ ] Implement Decisions 11 and 12. Snapshot. Changelog **Added** (the three exports) and **Changed** (`calls`).
- [ ] Commit (one per item).

### Task 9: Verify

- [ ] Every gate from scratch, the docs build, and a grep for each removed spelling (`CircuitBreaker`,
  `breaker_for`, `.breakers(`, `record_first`, `try_spend`, `refund(`, `wait_after`, `.delay(`, `cache_degradations`,
  `CACHE_DEGRADATIONS`, `headgate.policy`, `AddressGuard`, `PinnedHTTPTransport`, `resolve_with_loop`, `bind_guard`,
  `caches_request`, `stores_status`, `parse_link_header`, `Link` as a name, `.response()` on a `Respond`,
  `BodyFactory`) in `docs`, `README.md`, `examples`, `benchmarks`, `src` and `tests`, which should show only private
  uses, the changelog and specs.
