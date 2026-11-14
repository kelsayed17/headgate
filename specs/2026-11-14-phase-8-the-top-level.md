# Phase 8: The top level — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `headgate` exports what a client is written with, about 50 names, and every other public name lives in exactly one subpackage. No object is exported from two public modules.

**Architecture:**
- **One home per name.** Each public object is listed in the `__all__` of exactly one public module, and the docs name it there. The top level keeps design section 8's table. The pagers, the finer errors and `Problem` leave it. The subpackages stop re-exporting what the top level exports (`headgate.retry`'s `RetryPolicy`, `headgate.errors`' `ClientError`, and the rest). A test enforces the rule from now on.
- **Two constants go.** `DEFAULT_TIMEOUTS` becomes `Timeouts.DEFAULT`. `MAX_MINT_RESPONSE_BYTES` becomes private, with its value stated in the auth guide.
- **The fault steps live once.** `headgate.testing.faults` becomes the private `headgate.testing._faults`, since `headgate.testing` already exports every name it holds.

**Tech Stack:** Python 3.14, httpx2 2.16, ty, ruff, pytest, syrupy (API snapshot).

**Spec:** `specs/2026-11-05-public-api-redesign-design.md`, section 8. Phases 1–7a have merged: #56, #60, #85, #94, #100 and #101 for phases 1–5, #97 for phase 6, #99 for 7a. This plan assumes phase 7b has merged too (see **Coordination with phase 7b**).

## Global Constraints

- Python 3.14 or newer; httpx2 2.16 or newer; hishel 2.
- Base branch: `origin/main` after phase 7b has merged. Phase 8 touches an import line in most test files and every public `__init__.py`, so it cannot run in parallel with any other phase's code.
- Clean break: no deprecation aliases, and no old import path kept next to the new one. A removed re-export raises `ImportError`. A name its defining module still holds but no longer lists in `__all__` (a top-level error in `headgate.errors`, see Decisions) still imports, but its documented home is the top level.
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
- Each task adds a `CHANGELOG.md` entry under `## [Unreleased]`'s existing **Changed** or **Removed** section. Each entry says what to write instead.
- Docstrings describe what the code does now, in the present tense.
- The property tests in `tests/retry/test_properties.py` keep their properties and assertions. Only their import lines change.
- Out of scope: any change of behavior. This phase moves names; no call, default or message changes, except the messages that name a moved name.

## Coordination with phase 7b

Phase 7b (streams, pages, QUERY, nested query values) is being coded now, from `main` at `b0b383b`. It merges before any of this phase's code. From this plan's point of view it changes the top level as follows:

| 7b's change | What phase 8 does with it |
| --- | --- |
| `PageLimit` is removed from `headgate` and `headgate.pagination` | nothing: it is already gone |
| `Paginated[P, I]` becomes `Paginated[I, P = object]` | keeps `Paginated` at the top level, as design 8 says, and drops it from `headgate.pagination`'s `__all__` |
| `Method` gains `"QUERY"` | keeps `Method` at the top level; its value is 7b's |
| `Client.query()` and `Client.events()` | nothing: they are methods, not names. `client.events()` and the `headgate.events` module do not collide, since one is an attribute of a `Client` and the other of the package |
| `stream()`'s and `events()`'s options stay `headgate.streaming.StreamOptions` | nothing: it already has one home |
| `paginate(max_pages=, on_limit=)` | nothing: keywords, not names |

If 7b adds a public name this table does not list, Task 2's test reports it, and the name is placed by design 8's rule before the PR opens.

## Public surface after this phase

### `headgate` (52 names)

| Area | Names |
| --- | --- |
| The client | `Client`, `Method`, `RequestOptions`, `CallOptions`, `ViewOptions` |
| Configuration | `TransportPolicy`, `HttpOptions`, `SecurityOptions`, `Timeouts`, `Route`, `RouteOptions`, `IdempotencyKeys`, `ResponseCheck` |
| Middleware | `Middleware`, `Next`, `call_info` |
| Policy values | `RetryPolicy`, `BreakerPolicy`, `Rate`, `RateBudget`, `HttpCache` |
| Responses | `Expect`, `Reader`, `as_json`, `as_bytes`, `body_reader`, `nothing`, `event_stream` |
| Codecs | `Codec`, `reader_for`, `writer_for`, `decode_with` |
| Bodies | `Body`, `EncodedBody`, `Json`, `Form`, `Multipart`, `Raw`, `Writer` |
| Auth | `BearerAuth`, `StaticHeaderAuth`, `QueryParamAuth`, `SigningAuth`, `CachedTokenAuth`, `Minted` |
| Pages | `Paginated` |
| Errors | `ClientError`, `TransportError`, `ClientTimeoutError`, `UpstreamStatusError`, `AuthError`, `RateLimitError` |

That is design 8's table plus three names it does not list (`call_info`, `ResponseCheck` and `decode_with`; see Decisions). `Writer` moves from the Responses row to Bodies, beside `EncodedBody`, which it returns. Only the grouping changes.

### Leaving the top level

| Name | Its one home |
| --- | --- |
| `BodyCursor`, `BodyOffset`, `Cursor`, `LinkHeader`, `NextUrl`, `Offset`, `PageNumber` | `headgate.pagination` |
| `CircuitOpenError`, `ClientConfigurationError`, `DecodeError`, `ForbiddenDestinationError`, `PageLimitError`, `ResponseParseError`, `ResponseTooLargeError`, `Problem` | `headgate.errors` |
| `DEFAULT_TIMEOUTS` | `Timeouts.DEFAULT`, a class attribute |
| `MAX_MINT_RESPONSE_BYTES` | private; the auth guide states its value, 1 MiB |

### Leaving a subpackage's `__all__`, because the top level is their home

| Name | Leaves |
| --- | --- |
| `RetryPolicy` | `headgate.retry` |
| `Rate`, `RateBudget` | `headgate.ratelimit` |
| `BreakerPolicy` | `headgate.breaker` |
| `HttpCache` | `headgate.cache` |
| `Paginated` | `headgate.pagination` |
| `ClientError`, `TransportError`, `ClientTimeoutError`, `UpstreamStatusError`, `AuthError`, `RateLimitError` | `headgate.errors` (still defined there, see Decisions) |
| every name of `headgate.testing.faults` | the module, which becomes `headgate.testing._faults`; `headgate.testing` exports them all already |

## Migration

| Before | After | The old spelling now |
| --- | --- | --- |
| `from headgate import Cursor` (and every pager) | `from headgate.pagination import Cursor` | `ImportError` |
| `from headgate import ResponseParseError` (and every error but the six) | `from headgate.errors import ResponseParseError` | `ImportError` |
| `from headgate import Problem` | `from headgate.errors import Problem` | `ImportError` |
| `from headgate.retry import RetryPolicy` (and `ratelimit`'s `Rate`/`RateBudget`, `breaker`'s `BreakerPolicy`, `cache`'s `HttpCache`, `pagination`'s `Paginated`) | `from headgate import RetryPolicy` | `ImportError` |
| `from headgate.errors import ClientError` (and the five other top-level errors) | `from headgate import ClientError` | still imports, since `headgate.errors` defines it, but the docs and `__all__` name the top level |
| `DEFAULT_TIMEOUTS` | `Timeouts.DEFAULT` | `ImportError` |
| `MAX_MINT_RESPONSE_BYTES` | nothing: no option sets it; the auth guide says it is 1 MiB | `ImportError` |
| `from headgate.testing.faults import Respond` | `from headgate.testing import Respond` | `ModuleNotFoundError` |

## Decisions this plan makes

The design leaves these to the builder, or does not settle them. Each is the planner's call, made by the maintainer's rule: the cleanest design, even where it breaks things.

- **"Exported from two places" means listed in two public modules' `__all__`.** That is what a type checker treats as re-exported, what `import *` gives and what the API snapshot records. So the rule bites the subpackages too: today `RetryPolicy` is in both `headgate.__all__` and `headgate.retry.__all__`, and so are 27 other names. The top level is the home of its table's names, and each subpackage drops them.
  - A subpackage `__init__` that re-exported a name drops the import as well, so the old path raises `ImportError`. ruff's F401 would flag an import kept without the export anyway.
  - `headgate.errors` is a module, not a package, and it defines the whole error tree. The six top-level errors stay defined there, so `from headgate.errors import ClientError` still works at run time. They leave its `__all__`, and the docs name them `headgate.ClientError`. Moving their definitions out would split the error tree's one docstring and one `__reduce__` across two modules for no reader's gain.
  - The test compares objects, not names. `headgate.msgspec.reader` and `headgate.pydantic.reader` share a name but are two objects, which is the point of the two codecs.
- **Three names the design's table does not list stay at the top level.** Each has no subpackage to live in, and goal 4 forbids making a seam private.
  - `call_info`: design 5.1 put it there, beside `Middleware` and `Next`, which phase 5 built after the table was written.
  - `ResponseCheck`: the type of `HttpOptions(response_check=)`, which an SDK annotates its check with.
  - `decode_with`: the codec row's third builder, beside `reader_for` and `writer_for`. `docs/codecs.md` documents it for a document nested in a body.
- **`Timeouts.DEFAULT` is a `ClassVar`**, assigned after the class: `Timeouts.DEFAULT = Timeouts(connect=10.0, read=30.0, write=10.0, pool=15.0)`. It is the same frozen value `DEFAULT_TIMEOUTS` was, read where `DEFAULT_TIMEOUTS` is read today (`_options.py` resolves every unset phase against it). It does not appear in `dataclasses.fields(Timeouts)`, so `replace()` and the snapshot's field list are unchanged.
- **`MAX_MINT_RESPONSE_BYTES` becomes `_MAX_MINT_RESPONSE_BYTES`** in `_auth.py`. `docs/auth.md` states the value ("1 MiB, decompressed").
- **`headgate.testing.faults` becomes `headgate.testing._faults`.** Every name it exports is already in `headgate.testing.__all__`, and it is not one of the snapshot's public modules, so a second public path to the same objects was an accident. This is the same rule applied to the test kit.
- **The top-level docstring** lists the names by the table's rows and names the subpackages that hold everything else, as it does today. It is rewritten in Task 6 to match.

## Review Focus

These cases are the most likely to bite users. Each gets a test in the task named.

1. **One home per name.**
   - A test walks every module in `_PUBLIC_MODULES` and fails, naming both modules, when one object is in two of their `__all__` lists.
   - The test fails on `main` today: 28 names, from `AuthError` to `UpstreamStatusError` (27 once 7b removes `PageLimit`).
   - It passes once Tasks 3–5 are done.

   Covered in Task 2.
2. **The old paths fail as the Migration table says.**
   - `from headgate import Cursor` and `from headgate import ResponseParseError` raise `ImportError`.
   - `from headgate.retry import RetryPolicy` raises `ImportError`.
   - `import headgate.testing.faults` raises `ModuleNotFoundError`.
   - `hasattr(headgate, "DEFAULT_TIMEOUTS")` and `hasattr(headgate, "MAX_MINT_RESPONSE_BYTES")` are false.

   Covered in Tasks 3–5, a test per task.
3. **`Timeouts.DEFAULT` resolves as before.** `Timeouts(read=60.0).over(Timeouts.DEFAULT)` gives connect 10, read 60, write 10 and pool 15. `Timeouts.DEFAULT` is not a field, so `dataclasses.replace(Timeouts.DEFAULT, read=5.0)` builds a `Timeouts` and leaves the default alone. Covered in Task 5.
4. **The docs name each name where it lives.** The docs test that every exported name appears in `docs/` also checks that every `from headgate... import X` in a guide's or the README's code imports `X` from its one home. Today's check, that each imported name exists, passes for a name imported from its old home until the import itself is removed. Covered in Task 6.

---

### Task 1: Branch, and commit this plan

**Files:**
- Create: `specs/2026-11-14-phase-8-the-top-level.md` (this plan).
- Modify: `specs/2026-11-05-public-api-redesign-design.md`, section 8:
  - the table gains `call_info`, `ResponseCheck` and `decode_with`;
  - "exported from two places" is defined as two public modules' `__all__`;
  - `headgate.testing.faults` becomes private.

  Mark it "(Amended with the phase 8 plan.)".

- [ ] **Step 1:** Work in `claude/plan-phase-8`, cut from `origin/main` at `b0b383b`.
- [ ] **Step 2:** Run `uv run typos specs/` and `npx markdownlint-cli2 specs/2026-11-05-public-api-redesign-design.md`. Expected: no output.
- [ ] **Step 3:** Commit: `Add the phase 8 plan of the API redesign`. Open it as a draft PR for review. `src/` stays untouched until the plan is approved and phase 7b has merged.

### Task 2: A test that each name has one home

**Files:**
- Create: `tests/package/test_one_home.py`. For every pair of modules in `_PUBLIC_MODULES` (imported from `test_api_surface.py`), it fails on an object listed in both `__all__` lists, naming the object and both modules.

- [ ] **Step 1:** Rebase this branch onto `origin/main` after phase 7b has merged. Then write the test.
- [ ] **Step 2:** Run it. Expected: FAIL, listing 27 names.
- [ ] **Step 3:** Commit it marked `xfail(strict=True)`, with the reason "Tasks 3–5 move the names". Task 5 removes the mark.

### Task 3: The pagers and the finer errors leave the top level

**Files:**
- Modify: `src/headgate/__init__.py`: drop the seven pagers, the seven errors and `Problem` from the imports and `__all__`.
- Modify: every import of those names from `headgate` in `src/`, `tests/`, `benchmarks/`, `examples/`, `docs/` and `README.md`. Find them with `grep -rnE "from headgate import" src tests benchmarks examples docs README.md`. `uv run ruff check` and `ty` find what the grep misses.
- Test: `tests/package/test_one_home.py` gains the `ImportError` cases for this task (Review Focus 2).

- [ ] **Step 1:** Write the failing `ImportError` tests.
- [ ] **Step 2:** Make the change, then the mechanical import pass. Run every gate, and update the API snapshot.
- [ ] **Step 3:** CHANGELOG under **Removed**: the fifteen names, from `headgate`, with their homes. Commit: `Keep the pagers in headgate.pagination and the finer errors in headgate.errors`.

### Task 4: The subpackages stop re-exporting the top level's names

**Files:**
- Modify: `src/headgate/retry/__init__.py`, `ratelimit/__init__.py`, `breaker/__init__.py`, `cache/__init__.py` and `pagination/__init__.py`: drop each name the table puts at the top level, import and `__all__` both.
- Modify: `src/headgate/errors.py`: drop the six top-level errors from `__all__`. The module docstring says they are exported from `headgate`.
- Modify: every import of those names from their subpackage, as in Task 3 (148 imports in 87 files on `main` today, most of them `Rate` and `RetryPolicy` in tests). Source modules inside `headgate` import from the private module that defines the name (`headgate.retry._policy`), never from the top level, to keep the import graph acyclic.
- Test: the `ImportError` cases for this task (Review Focus 2).

- [ ] **Step 1:** Write the failing tests.
- [ ] **Step 2:** Make the change, then the import pass. Run every gate, and update the API snapshot.
- [ ] **Step 3:** CHANGELOG under **Changed**: each name is imported from `headgate` only, with the list. Commit: `Export each top-level name from the top level only`.

### Task 5: `Timeouts.DEFAULT`, a private mint cap, and one home for the fault steps

**Files:**
- Modify: `src/headgate/_options.py`: `DEFAULT_TIMEOUTS` becomes `Timeouts.DEFAULT`, a `ClassVar` assigned after the class. Every reader of it follows.
- Modify: `src/headgate/_auth.py`: `_MAX_MINT_RESPONSE_BYTES`.
- Move: `src/headgate/testing/faults.py` to `src/headgate/testing/_faults.py`. `headgate.testing`, `tests/retry/test_faults.py` and `docs/mutation-testing.md`'s file paths follow.
- Modify: `docs/configuration.md` (the default timeouts), `docs/auth.md` (the cap's value), `docs/testing.md`.
- Test: Review Focus 2's remaining cases, and Review Focus 3. Remove Task 2's `xfail` mark.

- [ ] **Step 1:** Write the failing tests.
- [ ] **Step 2:** Make the change. Run every gate, with `test_one_home.py` now passing, and update the API snapshot.
- [ ] **Step 3:** CHANGELOG under **Changed**: `Timeouts.DEFAULT` for `DEFAULT_TIMEOUTS`. Under **Removed**: `MAX_MINT_RESPONSE_BYTES`, and `headgate.testing.faults` (import from `headgate.testing`). Commit: `Fold the default timeouts into Timeouts, and give the mint cap and the fault steps one home`.

### Task 6: The top-level docstring, the docs, and the PR

**Files:**
- Modify: `src/headgate/__init__.py`'s docstring, by the table's rows.
- Modify: `tests/examples/test_docs.py`: a guide's or the README's import of a name from anywhere but its one home fails the test (Review Focus 4).
- Modify: `README.md` and `docs/`, wherever the new check finds a stale import; `docs/index.md`'s overview of what the top level holds.

- [ ] **Step 1:** Write the docs check, and run it. Expected: FAIL wherever a guide still imports from an old home.
- [ ] **Step 2:** Fix the guides. Run every gate, the docs build (`uv run --locked --only-group docs zensical build --strict --clean`) and the benchmarks against `origin/main`.
- [ ] **Step 3:** Run `grep -rnE "DEFAULT_TIMEOUTS|MAX_MINT_RESPONSE_BYTES|testing\.faults|from headgate import .*(Cursor|LinkHeader|Offset|PageNumber|NextUrl|Problem|ResponseParseError)" src tests docs examples README.md benchmarks .github pyproject.toml`. Expected: matches only in CHANGELOG entries and the tests that the old paths fail. `.github` and `pyproject.toml` are included because phase 5's grep missed `.github/workflows/ci.yml`.
- [ ] **Step 4:** Dispatch the whole-branch review. Fix what it finds, as separate commits.
- [ ] **Step 5:** Commit: `Describe the top level by what a client is written with`. Push and open the PR, titled `Phase 8 of the API redesign: the top level`. Its body lists the Migration table, names the phase 7b merge it is based on, and links the design's section 8. Do not merge.
