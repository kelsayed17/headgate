# Phase 7: Streams, pages and smaller items — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A stream's deadline bounds only opening it, and a timeout can say "no limit". Server-sent events open with one call. A paginated walk's type names its items first, and its page budget is two keywords that collide with no API's query. `Offset` can send the page size. QUERY is a verb. Every option is checked for its kind, and a nested query value is refused instead of being sent as its Python `repr`.

**Architecture:**
- **Streams.** A `stream()` call's read timeout is neither checked against the deadline nor cut to it, since the deadline ends when the stream opens. `client.events(url, ...)` is `stream()` with `Accept: text/event-stream` and no read limit.
- **"No limit".** `math.inf` in a `Timeouts` phase means no limit, and `None` keeps meaning "inherit". The client hands httpx2 `None` for that phase.
- **Pages.** `Paginated[I, P = object]` names the item type first. `paginate(max_pages=, on_limit=)` replaces `limit=PageLimit(...)`, and `PageLimit` is removed. `Offset(size_param=...)` sends the page size under that query parameter, or reads it from the caller's own (#53).
- **QUERY.** A `query()` verb, with a body like `post()`. QUERY joins the idempotent methods, so it is retried and hedged like GET, and OpenTelemetry's known methods.
- **Checks.** `max_pages`, `on_limit` and `size_param` are kind-checked. A query value that is a mapping, a set, bytes, or a sequence inside a sequence raises `TypeError`.

**Tech Stack:** Python 3.14, httpx2 2.16, ty, ruff, pytest, syrupy (the API snapshot).

**Spec:** `specs/2026-11-05-public-api-redesign-design.md`, section 7, and issue #53. Phases 1–4 and 6 have merged (#56, #60, #85, #94, #97). Phase 5 (#96) is being coded.

## Global Constraints

- Python 3.14 or newer; httpx2 2.16 or newer; hishel 2.
- Clean break: no deprecation aliases. `limit=` raises `TypeError` (an unknown keyword), and `PageLimit` raises `ImportError`.
- Gates for every task:
  - `uv run ruff check`
  - `uv run ruff format --check`
  - `uv run ty check`
  - `uv run typos`
  - `uv run python -m pytest --cov` at 100% line and branch coverage
  - No `noqa`, `ty: ignore` or `pragma: no cover`.
- A change to the public API updates the API snapshot (`uv run python -m pytest tests/package/test_api_surface.py --snapshot-update`), and the diff is reviewed.
- Every exported name appears in `docs/`, and a test enforces it.
- American English in code, docs and messages.
- Each task adds a `CHANGELOG.md` entry under `## [Unreleased]`'s existing **Added**, **Changed** or **Removed** section, and says what to write instead.
- Docstrings describe what the code does now, in the present tense.
- The property tests in `tests/retry/test_properties.py` keep their properties and assertions unchanged.
- The verb-path, pagination and stream benchmarks (`benchmarks/`) show no regression against `origin/main`.
- Out of scope:
  - Pager constructors that bind the page type from the reader (`Cursor.of(reader, ...)`). The design leaves them to the builder. They are additive, so they can follow on the new `Paginated` without breaking anything.
  - A default `$order` for a SODA walk, which #53 leaves at the call site.
  - Caching QUERY responses. `HttpCache` keeps caching GET only.
  - The top-level trim (phase 8): a name this phase adds is exported where its neighbors are.

## Coordination with phase 5

Phase 5 (#96) is rewriting `_client.py` now. Its Task 6 renames `AsyncBaseClient` to `Client`, rewrites `paginate`'s body, and moves every test, guide and example to a held `Client`. So this phase ships as two PRs:

- **7a starts now: Tasks 2 and 3.** They touch only `pagination/_pager.py`, `_options.py` and their tests and docs. Phase 5 changes neither of those source files.
  - The overlap with Task 6's mechanical pass is text in `docs/pagination.md` and `docs/configuration.md`. 7a's tests use the test helpers as they are on `main` when it opens.
- **7b waits for Phase 5's Task 6 to merge: Tasks 4 to 8.** Each changes `_client.py`:
  - the stream's deadline in `_prepare`, `events()` and `query()` beside the other verbs;
  - `paginate`'s signature, which Task 6 rewrites, and `_query_pairs`.
  - Task 7 also changes `retry/_policy.py`, where Phase 5's Task 4 adds `RetryShare`.
  - 7b is written against Phase 5's `Client`. Its new methods are methods of `Client`, its tests build a held `Client`, and its docs show one.

## Public signatures after this phase

Everything not shown is unchanged. `Client` is Phase 5's.

```python
# headgate (top level)
type Method = Literal["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "QUERY"]  # + QUERY


class Timeouts:  # a phase set to math.inf has no limit; None still inherits
    connect: float | None = None
    read: float | None = None
    write: float | None = None
    pool: float | None = None


@final
class Client:
    async def query[T](
        self, url: CallUrl, *, expect: Reader[T] | Expect[T], **options: Unpack[RequestOptions]
    ) -> T: ...  # new
    def events(
        self, url: CallUrl, *, last_event_id: str | None = None, **options: Unpack[StreamOptions]
    ) -> contextlib.AbstractAsyncContextManager[Stream]: ...  # new
    @overload
    def paginate[P, I](
        self,
        url: CallUrl,
        *,
        expect: Reader[P] | Expect[P],
        pages: Pager[P, I, None],
        max_pages: int | None = None,  # replaces limit=PageLimit(max_pages)
        on_limit: Literal["raise", "stop"] = "raise",  # replaces PageLimit(on_limit=)
        **options: Unpack[CallOptions],
    ) -> Paginated[I, P]: ...  # was Paginated[P, I]

    # ...and the same for the overload with query=


# headgate (top level, and headgate.pagination)
class Paginated[I, P = object](Protocol): ...  # was Paginated[P, I]


# headgate.pagination
@dataclass(frozen=True, slots=True, kw_only=True)
class Offset[P, I, B = None](WritingPager[P, I, B]):
    items: Callable[[P], Sequence[I]]
    page_size: int
    param: str = "offset"
    size_param: str | None = None  # new: the query parameter that carries the page size
    start: int = 0
    total: Callable[[P], int | None] | None = None
    write: Writer[B] | None = None
```

Removed: `PageLimit`, from `headgate` and `headgate.pagination`.

## Migration

| Before | After |
| --- | --- |
| `client.paginate(..., limit=PageLimit(10))` | `client.paginate(..., max_pages=10)` |
| `limit=PageLimit(10, on_limit="stop")` | `max_pages=10, on_limit="stop"` |
| `-> Paginated[list[Issue], Issue]` | `-> Paginated[Issue]`, or `Paginated[Issue, list[Issue]]` where the page type matters. A type checker flags the old order wherever the page and item types differ. |
| `Offset(page_size=100)` with `params={"limit": 100}` written by hand | `Offset(page_size=100, size_param="limit")`. The caller's own `limit` still wins when given. |
| `stream(..., deadline=5)`: each read was cut to the 5 s the deadline left, so a stream quiet for longer timed out | The deadline bounds only opening the stream. Each read is bounded by its read timeout. |
| `stream(url, headers={"Accept": "text/event-stream"}, timeout=Timeouts(read=...))` for SSE | `events(url)` |
| `Timeouts(read=None)` to mean "no limit" (it inherits) | `Timeouts(read=math.inf)` |
| `params={"filter": {"a": 1}}` sent `filter=%7B%27a%27%3A+1%7D` | Raises `TypeError`, naming the parameter. Encode the value first. |
| `client.request("QUERY", ...)` through httpx2 directly | `client.query(url, body=..., expect=...)` |

## Decisions this plan makes

The design leaves these to the builder, or does not settle them. Each is the planner's call, made by the maintainer's rule: the cleanest design, even where it breaks things.

- **"No limit" is `math.inf`, not a new `UNBOUNDED` name.** The design asks for "a way to say no limit (`UNBOUNDED`)".
  - `math.inf` already means "never" in headgate's API: `Minted(token, math.inf)` is a token that never expires.
  - `Timeouts` already accepts it as positive, and it needs no new name at the top level, which phase 8 is trimming.
  - The client turns it into httpx2's `None` for that phase.
  - `check_timeout_fits` never refuses it, because no limit under a deadline is the deadline.
  - `fit_timeouts` still cuts it to the time left for a verb, as it cuts an unset phase.
  - The design's 7 is amended in Task 1.
- **A stream's read timeout is neither checked nor cut.** The design says only that it is not checked against the deadline. Cutting it is what makes a quiet stream fail.
  - `fit_timeouts` cuts every phase to the time left when an attempt starts. So a stream opened with `deadline=5` reads with at most a 5 s read timeout, long after the deadline stopped applying.
  - A stream call's context is marked `streamed`, and `fit_timeouts` leaves its read phase as requested. Its `connect`, `write` and `pool` phases are still cut, because they belong to opening.
  - Waiting for the response head is bounded by the call's deadline timer, which `stream()` already runs until the head checks pass. So an uncut read cannot make opening outlast the deadline.
  - A policy whose `timeout` is longer than its `deadline` is still refused at construction. That check is about the verbs, and a stream that needs a longer read passes it per call or uses `events()`.
- **`events()` is `stream()` with two defaults, not a new kind of object.** It returns the same `Stream` context manager, so `aiter_events()`, `last_event_id` and `retry` are where they already are.
  - It sends a GET.
  - It sends `Accept: text/event-stream` unless the caller's `headers=` name an `Accept`.
  - Its read timeout is `math.inf` unless the caller's `timeout=` sets `read`. A heartbeat-less event stream can be silent for minutes, and the deadline already bounds opening it.
- **`max_pages=` and `on_limit=`, and `PageLimit` removed.** `max_pages` must be `None` or an `int` of at least 1, with `bool` refused. `on_limit` must be `"raise"` or `"stop"`. `on_limit="stop"` without `max_pages` raises `ValueError`, since it says what happens at a limit that does not exist. These checks are the "kind checks" gap the design names for `paginate`.
- **`Paginated[I, P = object]`.** The protocol's parameters are reordered, and `P` defaults to `object`. `PageWalk`, the private class that implements it, keeps its own order. `pages()` yields `P`, so a walk typed `Paginated[Issue]` yields pages of type `object`, which is what a caller who named no page type asked for.
- **`Offset(size_param=...)` (#53, option 1).**
  - When set, the first request carries the caller's own size under `size_param` if they gave one (an `int` of at least 1, or a string of digits, read as `_position` reads an offset), and `page_size` otherwise.
  - Each next request keeps that size. A page shorter than the request's size is the last.
  - `page_size` stays required, as the default and as what a walk without `size_param` steps by.
  - An empty `size_param` is refused, and so is a `size_param` equal to `param`.
  - `BodyOffset` does not gain it, since its body is the caller's to write.
- **`query()` mirrors `post()`.** It takes `body=` and `expect=`, and its `RequestOptions` are the same.
  - QUERY joins `IDEMPOTENT_METHODS`, so a QUERY is retried after any retryable failure, even with a body written. It can also be hedged, since only POST and PATCH are refused there.
  - It is not cached.
  - `paginate` keeps walking GETs and POSTs. A walk by QUERY can follow, but nobody has asked for one.
- **Which query values are refused.** A `Mapping`, an `AbstractSet`, `bytes`, `bytearray` or `memoryview`, and a `list` or `tuple` inside a `list` or `tuple`. Each raises `TypeError` naming the parameter and the type it got, before anything is sent. A scalar httpx2 does not encode itself (a `UUID`, a `Decimal`, an `Enum` member, a `date`) is still sent as its `str()`, as today: that is its meaning, not its `repr`.

## Review Focus

These cases are not covered by the design's own examples and are the most likely to bite users. Each gets a test in the task named.

1. **A quiet stream outlives its deadline.**
   - Against a loopback server, `stream(..., deadline=0.2, timeout=Timeouts(read=2.0))` reads a body whose chunks are 0.5 s apart to the end.
   - Before this change it raised `ClientTimeoutError` at the first gap.
   - A server that never sends the head still fails at the deadline.

   Covered in Task 4.
2. **`events()`'s defaults, and the caller's overrides.** A recording transport sees `Accept: text/event-stream` and a `read` timeout of `None`. A caller's `headers={"Accept": ...}` and `timeout=Timeouts(read=10.0)` each win. Covered in Task 5.
3. **`size_param`.**
   - The caller's `params={"$limit": "50"}` steps the offset by 50, with no `page_size` sent over it.
   - With no `$limit`, `page_size` is sent.
   - A 30-item page under a size of 50 ends the walk.
   - `"$limit": "x"` and `size_param="offset"` (equal to `param`) are refused when the walk is built.

   Covered in Task 2.
4. **The page budget.**
   - `max_pages=1` raises `PageLimitError` past the first page, and `on_limit="stop"` ends quietly.
   - `limit=` raises `TypeError`.
   - `max_pages=True`, `max_pages=0` and `on_limit="stop"` without `max_pages` are refused, with no request sent.
   - `Paginated[Issue]` type-checks in a `ty` snippet test, as `tests/client/test_call_options_types.py` checks the call options.

   Covered in Task 6.
5. **QUERY.**
   - A QUERY answered 503 then 200 is retried with its body sent whole twice.
   - A QUERY is hedged under a `Hedge`.
   - An OpenTelemetry span for it is named `QUERY`.
   - `query()` without `body=` sends no body.

   Covered in Task 7.
6. **Nested query values.**
   - Each refused kind raises `TypeError` with the parameter's name.
   - A `UUID` and an `Enum` member are still sent as their `str()`.
   - A list of scalars still repeats the key.

   Covered in Task 8.
7. **`math.inf`.**
   - `Timeouts(read=math.inf)` reaches httpx2 as `read=None`.
   - `TransportPolicy(timeout=Timeouts(read=math.inf), deadline=5)` is accepted.
   - A verb under that policy has its read cut to the time left.

   Covered in Task 3.

---

### Task 1: Branch, and commit this plan

**Files:**
- Create: `specs/2026-11-14-phase-7-streams-pages-and-smaller-items.md` (this plan).
- Modify: `specs/2026-11-05-public-api-redesign-design.md`, section 7: "no limit" is `math.inf`; a stream's read timeout is neither checked nor cut; the `size_param` semantics; the refused query values. Mark it "(Amended with the phase 7 plan.)".

- [ ] **Step 1:** Work in `claude/plan-phase-7`, cut from `origin/main` at `f1b0c02` (#97).
- [ ] **Step 2:** Run `uv run typos specs/` and `uv run ruff format --check specs/`. Expected: no output.
- [ ] **Step 3:** Commit: `Add the phase 7 plan of the API redesign`. Open it as a draft PR for review. `src/` stays untouched until the plan is approved.

## PR 7a: starts now

### Task 2: `Offset(size_param=...)` (#53)

**Files:**
- Modify: `src/headgate/pagination/_pager.py`: `Offset.size_param`, its checks in `__post_init__`, `first()` and `next_page()`.
- Modify: `docs/pagination.md`: the `Offset` row and the paragraph saying the page size is the caller's to send.
- Test: `tests/pagination/test_paginate.py`.

- [ ] **Step 1: Write the failing tests** for Review Focus 3.
- [ ] **Step 2:** Run them. Expected: FAIL, because `Offset` takes no `size_param`.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run every gate and the pagination benchmark, and update the API snapshot.
- [ ] **Step 5:** CHANGELOG under **Added**: `Offset(size_param=...)`. Commit: `Let Offset send the page size, or read it from the caller's query`.

### Task 3: "No limit" for a timeout phase

**Files:**
- Modify: `src/headgate/_options.py`: `Timeouts`' docstring; `httpx_timeout` maps `math.inf` to `None`; `check_timeout_fits` skips it.
- Modify: `docs/configuration.md` and `docs/deadlines.md`.
- Test: `tests/client/test_options.py` and `tests/client/test_timeouts_fit_deadline.py`.

- [ ] **Step 1: Write the failing tests** for Review Focus 7.
- [ ] **Step 2:** Run them. Expected: FAIL, because httpx2 is handed `inf` and the policy check refuses it.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run every gate.
- [ ] **Step 5:** CHANGELOG under **Added**: `math.inf` as a phase's "no limit". Commit: `Let a timeout phase say no limit with math.inf`. Open PR 7a.

## PR 7b: after Phase 5's Task 6 has merged

### Task 4: A stream's read timeout is the stream's

**Files:**
- Modify: `src/headgate/_core/_context.py`: `CallContext.streamed`, and `fit_timeouts` leaves the read phase of a streamed call as requested.
- Modify: `src/headgate/_client.py`: `_prepare` marks a stream call's context, and skips `read` in its per-call `check_timeout_fits`.
- Modify: `docs/streaming.md` and `docs/deadlines.md`.
- Test: `tests/streaming/` (a loopback server sending spaced chunks) and `tests/core/test_context.py`.

- [ ] **Step 1: Write the failing tests** for Review Focus 1.
- [ ] **Step 2:** Run them. Expected: FAIL at the first gap.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run every gate and the stream benchmarks.
- [ ] **Step 5:** CHANGELOG under **Changed**: a stream's reads are bounded by the read timeout alone, not by what the deadline left. Commit: `Bound a stream's reads by its read timeout, not the deadline`.

### Task 5: `events()`

**Files:**
- Modify: `src/headgate/_client.py`: `Client.events`, which builds on `stream()`.
- Modify: `docs/streaming.md`: the server-sent events section opens with `events()`.
- Test: `tests/streaming/test_events.py` (new, Review Focus 2) and the docs-exports test.

- [ ] **Step 1: Write the failing tests.**
- [ ] **Step 2:** Run them. Expected: FAIL, because there is no `events()`.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run every gate, and update the API snapshot.
- [ ] **Step 5:** CHANGELOG under **Added**. Commit: `Open server-sent events with one call`.

### Task 6: Pages: `Paginated[I, P]`, `max_pages=` and `on_limit=`

**Files:**
- Modify: `src/headgate/pagination/_paginated.py`: `Paginated[I, P = object]`. `PageLimit` is removed. `PageWalk` takes `max_pages` and `on_limit`, checked as in Decisions.
- Modify: `src/headgate/pagination/__init__.py` and `src/headgate/__init__.py`: drop `PageLimit`.
- Modify: `src/headgate/_client.py`: `paginate`'s overloads and body.
- Modify: `src/headgate/errors.py`: `PageLimitError`'s docstring.
- Modify: `docs/pagination.md`, `docs/errors.md`, `docs/retries.md`, `README.md` and `examples/` (five `Paginated[...]` annotations today).
- Test: `tests/pagination/test_paginate.py`, and a `ty` snippet test in the style of `tests/client/test_call_options_types.py`.

- [ ] **Step 1: Write the failing tests** for Review Focus 4.
- [ ] **Step 2:** Run them. Expected: FAIL.
- [ ] **Step 3:** Implement, then update every annotation that `uv run ty check` flags.
- [ ] **Step 4:** Run every gate and the pagination benchmark, and update the API snapshot.
- [ ] **Step 5:** CHANGELOG under **Changed**: `Paginated[I, P = object]`, and `max_pages=`/`on_limit=` for `limit=PageLimit(...)`. Under **Removed**: `PageLimit`. Commit: `Name a walk's items first, and give its page budget two keywords`.

### Task 7: QUERY

**Files:**
- Modify: `src/headgate/_client.py`: `Method` gains `"QUERY"`, and `Client.query`.
- Modify: `src/headgate/retry/_policy.py`: `IDEMPOTENT_METHODS` gains QUERY.
- Modify: `src/headgate/otel.py`: `_KNOWN_METHODS` gains QUERY.
- Modify: `docs/retries.md` (the idempotent methods), `docs/standards.md` (the RFC 9110 §9.2.2 row) and the verbs table in `docs/configuration.md`.
- Test: `tests/client/test_verbs.py`, `tests/retry/test_client_retries.py`, `tests/integration/test_hedging.py` and `tests/observe/`.

- [ ] **Step 1: Write the failing tests** for Review Focus 5.
- [ ] **Step 2:** Run them. Expected: FAIL, because there is no `query()`.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run every gate, and update the API snapshot.
- [ ] **Step 5:** CHANGELOG under **Added**. Commit: `Add QUERY, a safe verb with a body`.

### Task 8: A nested query value is refused

**Files:**
- Modify: `src/headgate/_client.py`: `_query_pairs` and `_query_value` refuse the kinds named in Decisions.
- Modify: `docs/configuration.md` (`params=`).
- Test: `tests/client/test_call_options.py`.

- [ ] **Step 1: Write the failing tests** for Review Focus 6.
- [ ] **Step 2:** Run them. Expected: FAIL, because a mapping is sent as its `repr`.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run every gate.
- [ ] **Step 5:** CHANGELOG under **Changed**: a nested `params` value raises `TypeError`, with how to encode one. Commit: `Refuse a nested query value instead of sending its repr`.

### Task 9: Verify each PR and open it

- [ ] **Step 1:** Rebase onto `origin/main`. Run every gate, the benchmarks against `origin/main`, and `uv run python -m pytest tests/retry/test_properties.py` on its own.
- [ ] **Step 2:** Read the API-snapshot diff whole. Every changed line is one this plan names.
- [ ] **Step 3:** Open the PR. Its body lists the Migration rows it covers and each Review Focus test by name. 7b's body also names the Phase 5 merge it is based on.
