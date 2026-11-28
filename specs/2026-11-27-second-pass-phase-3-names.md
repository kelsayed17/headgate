# Second pass, phase 3: One name per concept — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Each concept has one name and one home: the walk's value is `payload`, a pager carries its reader, breakers
take the rate's shape, `CallPolicy` holds the rate, the smaller one-name fixes land, and every exported class and
function reports the public module it is imported from.

**Architecture:** Twelve tasks, one per item of section 3 (3.5 split by area), each leaving `main`-quality green. The
pagers gain a positional `read` field before a `KW_ONLY` marker, so ty binds `P` from the reader and checks every
lambda against it; `paginate` reads `pager.read`. `Paginated` becomes the concrete `@final` walk, generic in
`[I, P]` only, with the pager's body type closed over. A private helper sets `__module__` on every class and function
a public module exports, and the six top-level errors move to a private module that `headgate.errors` does not bind.

**Tech Stack:** Python 3.14, httpx2 2.16, hishel 2, ty, ruff, pytest, syrupy (API snapshot).

**Spec:** `specs/2026-11-27-api-second-pass-design.md`, section 3 (3.1–3.6), approved 2026-11-27.

## Global Constraints

- Python 3.14 or newer; httpx2 2.16 or newer; hishel 2.
- Branch: `claude/api-second-pass-3-names`, stacked on `claude/api-second-pass-2-auth`. No push and no PR in this run.
- Clean break: no deprecation aliases, no old spelling kept next to the new one; never fix a finding by documenting it.
- Gates for every task: `uv run ruff check`, `uv run ruff format --check`, `uv run ty check`, `uv run typos`,
  `uv run python -m pytest --cov` at 100% line and branch coverage in one process, and
  `uv run zensical build --strict` once docs change. No `noqa`, `ty: ignore` or `pragma: no cover`.
- A public API change updates the snapshot (`uv run python -m pytest tests/package/test_api_surface.py --snapshot-update`), diff reviewed.
- Every exported name appears in `docs/`. American English. Docstrings in the present tense.
- Each task adds or extends a `CHANGELOG.md` entry under `## [Unreleased]`, saying what to write instead; an earlier
  phase's entry that names a renamed spelling is updated so the section reads as one release. Grep after every edit.
- `tests/retry/test_properties.py` keeps its properties and assertions; only spellings follow renames.
- Every rename reaches `docs/`, `README.md`, `examples/`, `benchmarks/` and `tests/`.

## Decisions

1. **A pager's reader is a positional dataclass field, `read: Reader[P] | Expect[P]`, then `_: KW_ONLY`.** A probe
   with ty (`Cursor(reader(Page), items=lambda p: p.items, ...)`) infers `Cursor[Item, Page, None]`, reports
   `p.itemz` as `Page has no attribute itemz`, and binds `B` from `write=`. `WritingPager` gains an abstract `read`
   property beside `write`; the `Pager` protocol gains a `read` property.
2. **`Paginated` is the concrete walk, `Paginated[I, P = object]`, `@final`.** Its constructor is
   `Paginated(pages, items, /)`: a zero-argument function that starts a fresh iteration of the pages
   (`Callable[[], AsyncIterator[P]]`) and the function that lists a page's items (`Callable[[P], Iterable[I]]`). The
   walk's logic, generic in the body type `B`, stays a private function in `pagination/_paginated.py` that `paginate`
   binds with `functools.partial`, so `B` never reaches the public class and no private type is in its signature.
   `PageWalk` and the `Paginated` protocol go.
3. **`paginate` checks `pager.read` as it checked `expect=`**, with the message naming `pager.read`; a pager without
   `read` raises `TypeError` naming the attribute. The rest of 5.1's pager check is phase 5's.
4. **`CircuitBreaker`, `RateBudget`, `RetryBudget` and `BreakerRegistry` read `current_timing()` when built.** Where
   headgate builds one itself later than the client (a registry's breaker on first use, a retry budget from a share,
   a rate budget from a `Rate`), it builds it inside `timing_override(timing)` with the timing it holds, so no private
   keyword replaces the public one.
5. **`HttpCache(methods=, statuses=)` stay typed `Collection[str]` and `Collection[int]`**, as phase 1 typed and
   checked them, with the values `frozenset({"GET"})` and `frozenset({200})` as defaults; the fields hold frozensets.
   `None` for either raises `TypeError` saying to leave the option out, or to write `cache=None` for no cache.
   (Amends 3.5's `frozenset` spelling of the type, not the defaults.)
6. **A client fills `CallInfo.full_url` for every call; its type stays `str | None`** (changed during
   implementation: the plan first made it a required `str`). `None` is left for a `CallInfo` built by hand, as tests
   of observers and every layer's own test build one, so no test of the retry properties changes beyond spellings.
   `CallInfo.template` replaces `route`.
7. **`as_text` lands here**: it is `Reader(lambda r: r.text)`, one line beside `as_events`, and 3.5's rename names it.
   5.3's refusal of `Reader(str)` pointing to it stays phase 5's.
8. **The six top-level errors move to `headgate/_errors.py`, with every other error, `ErrorDetails` and `Problem`.**
   `headgate.errors` imports the nine, `ErrorDetails` and `Problem` from it, and nothing else, so
   `from headgate.errors import ClientError` raises `ImportError`. Internal modules import the six from
   `headgate._errors`, and keep importing the nine from `headgate.errors`, so `import headgate` imports it.
9. **One helper sets every home.** `headgate._core.rehome(module_name, namespace)` sets `__module__` to
   *module_name* on every class and function the namespace's `__all__` names; every public module calls it last. A type
   alias (`type Body = ...`) cannot take a `__module__` (`TypeAliasType` refuses it) and is not a class or function,
   so the rule leaves it. The cache's credential identity reads an auth's `__module__.__qualname__`, so its key bytes
   change with the homes, which makes old entries misses and nothing else.

## Review Focus

1. **A lambda pager written as `docs/index.md` writes it** is checked by ty against the page type, and a walk's items
   are inferred, through `paginate` as well as on the pager. Covered in Task 3.
2. **A shared `BreakerRegistry` built inside `TestClock.install()` and used after the block** still trips and recovers
   on the test clock. Covered in Task 12.
3. **A pickled top-level error round-trips** once it reports `headgate` as its module. Covered in Task 13.
4. **`events(last_event_id="")`** sends no `Last-Event-ID`, as `stream()` did, after the header logic moves. Covered in
   Task 9.
5. **A route's `timeout=` longer than the deadline its calls run under** is refused where the route is given, as the
   policy's and a view's are. Covered in Task 6.

---

### Task 1: Commit the plan

- [ ] `uv run typos specs/`, then `git add specs/2026-11-27-second-pass-phase-3-names.md && git commit`.

### Task 2: The walk's value is `payload` (3.1)

**Files:** `pagination/_pager.py` (`PageRequest.payload`, `with_payload`, messages "pass payload="), `_client.py`
(`paginate(payload=)`, `_WALK_BODY` names `payload=`), `pagination/_paginated.py`; docs `pagination.md`,
`configuration.md`, `README.md`; tests.

- [ ] **Tests:** `test_a_walk_posts_its_payload` (a `BodyCursor` walk given `payload={"q": "x"}` POSTs it, and the
  next page's `request.payload` carries the cursor); `test_query_is_not_a_walk_keyword` (`paginate(..., query=...)`
  raises `TypeError` naming `payload=`).
- [ ] Rename everywhere (`body`/`with_body` on `PageRequest`; `query=` on `paginate`). Snapshot. Changelog **Changed**.
- [ ] Commit.

### Task 3: A pager carries its reader (3.2)

**Files:** `pagination/_pager.py`, `pagination/_paginated.py`, `_client.py`; every pager in docs, README, examples,
benchmarks and tests.

**Interfaces:**
- `Pager[I, P, B = None]` (protocol) with `read`, `first`, `page_items`, `next_page`; `WritingPager[I, P, B]`;
  `Cursor[I, P, B = None]`, `Offset[I, P, B = None]`, `PageNumber[I, P, B = None]`, `LinkHeader[I, P]`,
  `NextUrl[I, P]`, `BodyCursor[I, P, B]`, `BodyOffset[I, P, B]`, each taking `read` first and positionally, the rest keyword-only (`KW_ONLY`).
- `paginate[I, P](url, *, pager: Pager[I, P, None], max_pages=, on_limit=, **CallOptions) -> Paginated[I, P]` and
  `paginate[I, P, B](url, *, payload: B | None, pager: Pager[I, P, B], ...)`.

- [ ] **Tests:** `tests/pagination/test_pager_types.py` (ty, as `tests/decoding/test_reader_types.py` runs it): the
  `docs/index.md` form `LinkHeader(reader(list[Issue]), items=lambda page: page)` reveals `LinkHeader[Issue, list[Issue]]`,
  `client.paginate(..., pager=...)` reveals `Paginated[Issue, list[Issue]]`, and `lambda p: p.itemz` on a `Cursor`
  is one `unresolved-attribute`; `expect=` on `paginate` is `unknown-argument`. Runtime:
  `test_a_pager_without_read_is_refused`, `test_a_pager_reads_with_its_expect` (a pager whose `read` is an
  `Expect` with `accept=` sends that `Accept` on every page), `test_pages_is_not_a_walk_keyword`.
- [ ] Implement (Decision 1, 3); migrate every call site. Snapshot. Changelog **Changed** (one entry: reader first,
  `pager=`, no `expect=`, item-first generics, `read`).
- [ ] Commit.

### Task 4: `Paginated` is the walk (3.6, last sentence)

**Files:** `pagination/_paginated.py`, `_client.py`, `pagination/__init__.py` docstring, docs `pagination.md`.

- [ ] **Tests:** `test_paginate_returns_a_paginated` (`type(walk) is Paginated`, `Paginated.__final__`), and
  `await client.paginate(...)` raises `TypeError` whose message names `'Paginated'`.
- [ ] Implement Decision 2. Snapshot. Changelog **Changed** (F9's message).
- [ ] Commit.

### Task 5: Breakers take the rate's shape (3.3)

**Files:** `breaker/_policy.py`, `breaker/_registry.py`, `_client.py` (`_registry_of`, `_breaker_for`),
`_options.py` (policy `breaker` kind), `testing/_inspect.py` if it names `bind`; docs `breaker.md`, `configuration.md`;
tests.

**Interfaces:** `BreakerPolicy(*, failure_threshold=5, recovery_timeout=60.0)`;
`BreakerRegistry(policy: BreakerPolicy = BreakerPolicy(), /, *, max_hosts: int = 1024)` with `.policy`;
`breaker_for(origin)`; `CallPolicy.breaker: BreakerPolicy | BreakerRegistry | None`. A non-`BreakerPolicy` *policy*
raises `TypeError`.

- [ ] **Tests:** `test_clients_given_one_registry_share_its_breakers` (two clients, one registry with
  `BreakerPolicy(failure_threshold=2)`: one client's failures open the other's breaker);
  `test_a_registry_carries_its_thresholds` (no `bind`, `breaker_for(origin)` builds with the registry's policy);
  `test_a_policy_has_no_registry` (`BreakerPolicy(registry=...)` is a `TypeError`).
- [ ] Implement; remove `bind`, its `ValueError` and its tests. Snapshot. Changelog **Changed** and **Removed**.
- [ ] Commit.

### Task 6: `CallPolicy`, holding the rate (3.4)

**Files:** `_options.py` (`CallPolicy`, `RouteOptions.timeout`, `Route` checks), `_client.py` (no `rate=`; the route
timeout layered under the view's and the call's), `__init__.py`, `_stack.py` and every `TransportPolicy`; docs, README,
examples, benchmarks; tests.

**Interfaces:** `CallPolicy(*, rate: Rate | RateBudget | None = None, retry, timeout, deadline, breaker, cache,
idempotency, routes)`; `RouteOptions.timeout: Timeouts`.

- [ ] **Tests:** `test_the_policy_holds_the_rate` (a `CallPolicy(rate=Rate.per_second(1))` paces; `Client(rate=...)`
  is a `TypeError`); `test_a_route_sets_its_timeout` (a route's `Timeouts(read=...)` reaches the request's httpx2
  timeout on its paths only, and a call's `timeout=` wins); Review Focus 5 (`Route("/x", timeout=Timeouts(read=90))`
  under a 60 s deadline raises `ValueError`, also with the route's own `deadline=`).
- [ ] Implement. Snapshot. Changelog **Changed** (`TransportPolicy` → `CallPolicy`, `rate=` moves) and **Added**
  (`RouteOptions.timeout`).
- [ ] Commit.

### Task 7: One spelling of an answer and of a HEAD (3.5: `Expect`, `head`)

**Files:** `_responses.py` (`answers` is `field(init=False)`, filled by `on()` through a private copy), `_client.py`
(`head` has one signature, `expect=` required; `paginate`'s paired `Expect`); docs; tests.

- [ ] **Tests:** `test_expect_takes_no_answers` (`Expect(as_json, {404: nothing})` is a `TypeError`;
  `Expect(as_json).on(404, nothing).answers` holds it); ty: `client.head("x")` is `missing-argument`.
- [ ] Implement. Snapshot. Changelog **Changed**.
- [ ] Commit.

### Task 8: `call_view` and `CallInfo.template` (3.5)

**Files:** `_core/_context.py`, `_core/_events.py`, `_core/__init__.py`, `_client.py` (fill `full_url` always),
`otel.py`, `__init__.py`; docs `configuration.md`, `observability.md`; tests.

**Interfaces:** `call_view(request) -> CallView | None`; `CallView(info, attempt, remaining)`;
`CallInfo(*, method, url, full_url: str, template: str | None = None, call_id)`.

- [ ] **Tests:** `test_a_middleware_reads_its_call_view` (`call_view(r).info.template == "items/{uid}"`);
  `test_full_url_is_filled_without_observers` (E18: a client with no observers, `call_view(r).info.full_url` is the
  absolute sanitized URL).
- [ ] Implement. Snapshot. Changelog **Changed**.
- [ ] Commit.

### Task 9: Streams (3.5)

**Files:** `_client.py` (`stream` drops `last_event_id`; `events` sets the header), `streaming/_stream.py`
(`reconnect_after: float | None`, seconds; `aiter_bytes(*, chunk_size=None)`), `streaming/_events.py` if it holds the
retry; docs `streaming.md`; tests.

- [ ] **Tests:** `test_reconnect_after_is_in_seconds` (`retry: 2500` gives `2.5`); `test_stream_takes_no_last_event_id`
  (`TypeError`); Review Focus 4; ty: `aiter_bytes(10)` is an error.
- [ ] Implement. Snapshot. Changelog **Changed**.
- [ ] Commit.

### Task 10: Readers named as readers (3.5: `as_events`, `as_text`)

**Files:** `_responses.py`, `__init__.py`, docs `responses.md`, `streaming.md`; tests.

- [ ] **Tests:** `test_as_events_reads_every_event`; `test_as_text_decodes_with_the_charset` (a `text/plain;
  charset=latin-1` body `"naïve"` encoded in Latin-1 reads `"naïve"`).
- [ ] Implement. Snapshot. Changelog **Changed** (`event_stream` → `as_events`) and **Added** (`as_text`).
- [ ] Commit.

### Task 11: Cache names and defaults (3.5)

**Files:** `_core/_events.py` (`CacheOutcome.COALESCED = "coalesced"`, `CacheChecked`, `DeprecationNoticed`),
`cache/_policy.py` (`Partition(StrEnum)`, Decision 5), `cache/_transport.py`, `_deprecation.py`, `events.py`,
`otel.py`; docs `caching.md`, `observability.md`; tests.

- [ ] **Tests:** `test_a_coalesced_call_reports_coalesced`; `test_partition_is_a_str_enum`;
  `test_the_cache_defaults_are_values` (`HttpCache(storage=s).methods == frozenset({"GET"})`, `.statuses ==
  frozenset({200})`, `methods=None` is a `TypeError`).
- [ ] Implement. Snapshot. Changelog **Changed**.
- [ ] Commit.

### Task 12: Time has one seam (3.5)

**Files:** `ratelimit/_budget.py`, `ratelimit/_budgets.py`, `retry/_policy.py`, `retry/_transport.py`,
`breaker/_breaker.py`, `breaker/_registry.py`, `testing/_faults.py`, `_core/_time.py` and `timing.py` docstrings; docs
`testing.md`, `rate-budgets.md`, `breaker.md`; tests.

- [ ] **Tests:** `test_no_constructor_takes_timing` (`timing=` is a `TypeError` on the four; `sleep=` on
  `FaultyTransport`); `test_faulty_transport_sleeps_on_the_test_clock`; Review Focus 2.
- [ ] Implement Decision 4. Snapshot. Changelog **Changed**.
- [ ] Commit.

### Task 13: A class reports its one home (3.6)

**Files:** new `src/headgate/_errors.py`; `errors.py`; `__init__.py`; new `_core/_home.py` (`rehome`); every public
module's last line; every internal import of the six; docs `errors.md`; `tests/package/test_one_home.py`.

- [ ] **Tests:** `test_every_export_reports_its_home` (for every public module, every class and function in
  `__all__` has `__module__ == module`); `test_a_top_level_error_has_one_home` replaces
  `test_a_top_level_error_is_defined_in_errors_but_exported_from_the_top_level`
  (`from headgate.errors import ClientError` raises `ImportError`); `test_a_traceback_names_the_public_home`
  (`traceback.format_exception` of an `UpstreamStatusError` names `headgate.UpstreamStatusError`, and an
  `UnmatchedRequestError` `headgate.testing.UnmatchedRequestError`); Review Focus 3.
- [ ] Implement Decisions 8 and 9. Snapshot. Changelog **Changed**.
- [ ] Commit.

### Task 14: Verify

- [ ] Every gate from scratch, the docs build, and a grep for the old spellings (`TransportPolicy`, `call_info`,
  `event_stream`, `CacheResult`, `Deprecated(`, `SHARED` outcome, `pages=`, `query=` on `paginate`, `expect=` on
  `paginate`, `.registry`, `bind(`, `timing=`, `sleep=`, `last_event_id=` on `stream`, `.retry` on a stream,
  `.route` on a `CallInfo`) in `docs`, `README.md`, `examples`, `benchmarks`, `src` and `tests`, which should show
  only the changelog and specs.
