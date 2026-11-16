# Phase 3: Per-call options and the walk's `query=` — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give every call one set of options, so a wrapper typed on them can forward all of them. Give the request body one keyword whose value says what it is and whether it can be re-sent. Give a walk's body a keyword of its own, so `body=` means one thing everywhere. Let one call raise or lower the response-size cap.

**Architecture:** `retry`, `follow_redirects` and `cache_ttl` stop being named parameters and join the options `TypedDict`s, where leaving an option out is real absence. `None` is off, so a call's `retry=None` sends once. The base is a private `TypedDict` that is not closed, and the public bags are closed leaves of it: `CallOptions`, `RequestOptions` (plus `body=`), `headgate.streaming.StreamOptions` and `ViewOptions`. The five body keywords become one `body: Body | None`. `Body` is the union of `EncodedBody`, `Json`, `Form`, `Multipart` and `Raw`, each a small frozen class. `Raw` and `Multipart` say whether they can be re-sent (`.replayable`), and the client reads only that. `paginate(query=)` replaces the walk's `body=` and `json=`, and the pager's `write=` decides how the value is sent. `CallOptions(max_response_bytes=)` carries a per-call cap. The client checks it, and the cache layer reads it off the call's context.

**Tech Stack:** Python 3.14, httpx2 2.16, ty, ruff, pytest, syrupy (API snapshot).

**Spec:** `specs/2026-11-05-public-api-redesign-design.md`, section 3. Phase 1 merged as #56 and Phase 2 as #60. Two more items are folded in. The first builds on the write-safety fix below the retry layer: a body that is not replayable refuses a second pass at the stream. `Raw(...)` and `Multipart(...)` build on it by saying whether they can be re-sent. The second is #57: `CallOptions` takes `max_response_bytes`, which may raise the client's cap or lower it, and which applies to the cache's buffering too.

## Global Constraints

- Python 3.14 or newer; httpx2 2.16 or newer; hishel 2.
- Base branch: `origin/main` after the write-safety fix has merged. **Do not start Task 2 before it has.** Rebase onto `origin/main` again before Task 3 if the fix for the cache on the stream path has merged since, because Task 3 touches `cache/_transport.py`.
- Clean break: no deprecation aliases, and no old spelling kept next to the new one (the design's Migration section). Where an old spelling would otherwise be silently ignored at runtime, it raises a `TypeError` that names the new spelling (**Decisions**).
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
- The property tests in `tests/retry/test_properties.py` keep their properties and assertions. Only their spellings change (`retry=False` → `retry=None`, `content=` → `body=Raw(...)`).
- Out of scope:
  - `TransportPolicy(retry=)`, `NO_RETRY`, `Route` and `rate_limits` (phase 4). After this phase a call's `retry=None` sends once, but `Route(retry=None)` still means "no override" until phase 4 makes it "off" as well.
  - `limit=PageLimit(...)` (phase 7 renames it).
  - The `query()` verb (phase 7).
  - The top-level trim (phase 8).
  - `AsyncBaseClient` keeps its name until phase 5.

## Public signatures after this phase

Everything else on the client is unchanged.

```python
# headgate (top level)
class CallOptions(TypedDict, total=False, closed=True):
    params: Mapping[str, object] | None
    headers: Mapping[str, str]
    timeout: Timeouts
    deadline: float | None          # None: no deadline
    retry: RetryPolicy | None       # None: send once
    follow_redirects: bool
    cache_ttl: float | None         # None: store without a lifetime limit
    idempotency_key: str
    max_response_bytes: int         # new (#57)

class RequestOptions(TypedDict, total=False, closed=True):   # CallOptions, plus:
    body: Body | None               # None: no body

class ViewOptions(TypedDict, total=False, closed=True):
    headers: Mapping[str, str]
    timeout: Timeouts
    deadline: float | None
    retry: RetryPolicy | None       # was bool | RetryPolicy
    follow_redirects: bool          # new

type Body = EncodedBody | Json | Form | Multipart | Raw     # was the raw-content alias

@final @dataclass(frozen=True, slots=True)
class Json:      value: object
class Form:      fields: Mapping[str, object]
class Multipart: files: UploadFiles; fields: Mapping[str, object] = {}   # .replayable: bool
class Raw:       content: bytes | AsyncIterable[bytes] | Callable[[], AsyncIterable[bytes]]   # .replayable: bool

# headgate.streaming
class StreamOptions(TypedDict, total=False, closed=True):
    # CallOptions without cache_ttl and max_response_bytes, plus body: Body | None

# AsyncBaseClient
async def get[T](self, url: CallUrl, *, expect: Reader[T] | Expect[T], **options: Unpack[RequestOptions]) -> T
#   ...and the same for post, put, patch and delete; head keeps its two overloads, with expect= defaulting to nothing
def stream(self, url: CallUrl, *, method: Method = "GET", last_event_id: str | None = None,
           **options: Unpack[StreamOptions]) -> AbstractAsyncContextManager[Stream]
@overload
def paginate[P, I](self, url: CallUrl, *, expect: Reader[P] | Expect[P], pages: Pager[P, I, None],
                   limit: PageLimit | None = None, **options: Unpack[CallOptions]) -> Paginated[P, I]
@overload
def paginate[P, I, B](self, url: CallUrl, *, query: B | None, expect: Reader[P] | Expect[P],
                      pages: Pager[P, I, B], limit: PageLimit | None = None,
                      **options: Unpack[CallOptions]) -> Paginated[P, I]
def with_options(self, **options: Unpack[ViewOptions]) -> Self
```

Removed: `PageOptions`, and the verbs' and `stream`'s named `retry=`, `follow_redirects=` and `cache_ttl=` parameters. `RequestOptions`'s `json`, `data`, `content` and `files` are removed too, along with `paginate`'s `body=`, `json=` and named `retry=`. `check_one_body`, `request_content` and the old `Body` alias are private and go as well.

## Migration

Every row is a **Changed** or **Removed** entry in the CHANGELOG with this replacement. The last column says how the old spelling fails, since none of them is kept.

| Before | After | The old spelling now |
| --- | --- | --- |
| `retry=False` (call or view) | `retry=None` | `TypeError` naming `retry=None` |
| `retry=True` (call, undoing a view's) | make the call on the client, not the view | `TypeError` saying so |
| `retry=None` passed explicitly, meaning "inherit" | leave `retry` out | **sends once, silently**. See Review Focus 1. |
| `follow_redirects=None` | leave it out | `TypeError` naming `follow_redirects=` |
| `cache_ttl=None` passed explicitly, meaning "the cache's ttl" | leave it out | **stores with no lifetime limit, silently**. See Review Focus 1. |
| `json=value` | `body=Json(value)` | `TypeError` naming `body=Json(...)` |
| `data=fields` | `body=Form(fields)` | `TypeError` naming `body=Form(...)` |
| `files=files, data=fields` | `body=Multipart(files, fields)` | `TypeError` naming `body=Multipart(...)` |
| `content=raw` | `body=Raw(raw)` | `TypeError` naming `body=Raw(...)` |
| `body=WRITE(value)` on a verb | unchanged | — |
| `paginate(body=value)` / `paginate(json=value)` | `paginate(query=value)` | `TypeError` naming `query=` |
| `paginate(retry=...)` as a named parameter | unchanged spelling, now in `CallOptions` | — |
| `PageOptions` | `CallOptions` | `ImportError` |
| one size cap for the whole client | `max_response_bytes=` on the call, when one call needs another | — |

Two meanings change without an error: an explicit `retry=None` and an explicit `cache_ttl=None`. Each used to mean "no override" and now means "off". For `cache_ttl=None`, "off" means the same as `HttpCache(ttl=None)` does today: no headgate lifetime, so the entry is served until the storage evicts it. headgate's cache is TTL-only and ignores the response's own cache headers by design (`cache/_policy.py`: `Cache-Control: max-age`, `Expires` and even `no-store`). So a call's `cache_ttl=None` can keep an entry past the server's `max-age`, as a client-wide `ttl=None` already can. The CHANGELOG entry says so in those words, so nobody reads "no limit" as "the server's limit". So the CHANGELOG entry for each one opens with it, in bold, and gives the search that finds an affected call: `grep -rnE "(retry|cache_ttl)=(None|[a-z_]+)\b"`. A forwarding wrapper (`retry=retry`) whose own default is `None` is the usual case. The fix is to type the wrapper on `**options: Unpack[CallOptions]` and forward `**options`, which is what this phase makes possible.

## Decisions this plan makes

The design leaves these to the builder, or does not settle them. Each is the planner's call, made by the maintainer's rule: the cleanest design, even where it breaks things.

- **This phase applies 4.2's call-level "off" now**, as design 3.1 says ("Their values follow 4.2"). A `TypedDict` makes leaving an option out real absence, so the call and view `None` can mean off at once. The policy-level and route-level halves stay in phase 4. Until then, a call's `retry=None` maps to `NO_RETRY` internally.
- **The runtime refuses a keyword it does not know.** A closed `TypedDict` catches a stray keyword for a type-checked caller only. At runtime `**options` swallows it, so today `post(url, jsno=...)` sends a POST with no body. After this phase, the same would happen silently to every untyped `post(url, json=...)`. So every verb, `stream`, `paginate` and `with_options` checks `options.keys()` against its bag's keys (`__optional_keys__`, read once per bag at import time into a module-level `frozenset`) before anything else, and raises `TypeError: post() got an unexpected keyword argument 'json'`. For a keyword this phase removed, the message adds its replacement from the Migration table, for example `: write body=Json(value)`. The check is one `frozenset` comparison per call. The benchmarks in `benchmarks/` must show no regression on the verb path.
- **Kind checks at the call** (design 7's rule, started by #54) for the options this phase moves, each through `check_kind`:
  - `retry=` takes a `RetryPolicy` or `None`, and a `bool` gets the Migration table's message.
  - `follow_redirects=` takes a `bool`.
  - `cache_ttl=` takes a positive finite number or `None`, as `HttpCache(ttl=)` does.
  - `max_response_bytes=` takes a positive `int`, and a `bool` is refused, as `HttpOptions` checks it.
  - `body=` takes a `Body`, so `body=b"..."` names `Raw`.
  - `Raw(content)` checks its content when it is built, with today's `request_content` message, now naming `Raw`.
  - `Form(fields)` and `Multipart(files, fields)` check that `fields` is a `Mapping`.
- **`Multipart(files, fields={})`**, files first, because the files are what make a body multipart. The design writes `Multipart(fields, files)`. Its 3.3 table is amended to match, in Task 1.
- **`Raw.replayable` and `Multipart.replayable` are read-only properties derived from the value.** They are not constructor arguments: a caller cannot make an async iterator replayable, and a second spelling of the same fact would be two ways to say one thing.
  - `Raw`: `bytes` and a factory are `True`. Any other async iterable is `False`, as `request_content` decides today.
  - `Multipart`: returns `files_replayable(self.files)`, the helper the write-safety fix adds to `_request_body.py` (`False` when a file value is a file object that cannot be rewound). The property calls the helper and does not copy its rule, so there is one rule in one place.
  - `EncodedBody`, `Json` and `Form` are always bytes in memory, so they have no property.
  - `_prepare` stamps `REPLAYABLE_BODY` from this one fact, and the write-safety fix's stream refusal and hedge rule read `REPLAYABLE_BODY` as they already do. Phase 3 adds no other path that decides replayability.
- **`body=None` is no body**, the same as leaving it out. A body has nothing to inherit, so absence and `None` agree. `None` stays allowed so that a wrapper can forward an optional body.
- **`StreamOptions` lives in `headgate.streaming`, not at the top level.** A stream is never buffered, so it takes no `max_response_bytes` (the design's "`CallOptions` minus `cache_ttl`" is amended to "minus `cache_ttl` and `max_response_bytes`"). It still takes `body=`, for a POST stream. It is a public name, so a wrapper typed on it needs no private import (goal 4). Section 8's top-level table is unchanged.
- **`max_response_bytes` is a call option, not a view option.** #57's case is one file among many on one host, which is one call. `ViewOptions` stays the design's five. A view that needs another cap for all its calls is a later, additive change.
- **A per-call cap reaches the cache through the call's context.** `_prepare` puts the call's `max_response_bytes`, when given, on its `CallContext` as a new `Slot[int]` (`MAX_RESPONSE_BYTES`). `ResilientCacheTransport` buffers up to that value when the request carries one, and up to its own `max_body_bytes` otherwise. `read_capped` takes the same value. A cached entry is always checked against the cap of the call that reads it. So an entry stored under a raised cap and read later by a call with the default cap raises `ResponseTooLargeError`, as the same body from the network would.
- **A view with `follow_redirects=True` on a client with a `response_check` is refused by `with_options`** with the client constructor's `ValueError`, not only later at each call.
- **`paginate(query=)`, without a `write=`, is sent as JSON.** That is today's `json=`. With a `write=`, the pager encodes each page's value, which is today's `body=`. The two refusals that tied a keyword to a pager having or lacking `write=` go away, because the pager now decides. The fixed-idempotency-key refusal stays. `query=None` is a walk of GETs, as `body=None` is today.

## Review Focus

These six cases are not covered by the design's own examples and are the most likely to bite users. Each line gets a test in the task named.

1. **The two silent meaning changes.** A call's explicit `retry=None` on a view with a `RetryPolicy` sends once, while the same call without `retry` takes the view's policy. A call's explicit `cache_ttl=None` on a cache with `ttl=60` stores an entry that is still served after 61 seconds of `TestClock`, while the same call without `cache_ttl` stores one that has expired by then. A third case pins the cache's existing TTL-only rule under the new spelling: a `cache_ttl=None` entry whose response said `Cache-Control: max-age=1` is still served after 61 seconds, exactly as an entry of a `HttpCache(ttl=None)` client is. Covered in Task 2.
2. **Every removed keyword fails before anything is sent.** On each verb, `json=`, `data=`, `content=` and `files=` raise `TypeError` with their replacement, and the fake transport records no request. On `paginate`, `body=` and `json=` do the same, before the pager's `first()` runs. On a call or a view, `retry=True`, `retry=False` and `follow_redirects=None` do too. Covered in Tasks 2, 4 and 5.
3. **Forwarding type-checks.** A wrapper `async def fetch(self, url: LiteralString, **options: Unpack[CallOptions])` forwards `**options` to `get`, `post` and `paginate` with ty exit code 0. Forwarding `**options: Unpack[RequestOptions]` to `stream` is a type error, because `cache_ttl` is not a stream option. Covered in Task 2.
4. **The per-call cap is the call's cap at both layers, in both directions.** Each case runs without a cache and with one:
   - On a client capped at 1 KiB, a call with `max_response_bytes=4096` reads a 3 KiB body.
   - On a client capped at 4 KiB, a call with `max_response_bytes=1024` raises `ResponseTooLargeError` for a 3 KiB body.
   - A 3 KiB entry stored by a raised-cap call is served from the cache to that call again, and refused to a default-cap call.
   - `stream(max_response_bytes=...)` is a type error and a `TypeError`.
   Covered in Task 3.
5. **Replayability follows the body.**
   - `Raw(async_iterator)` is sent once and never re-sent: not after a retryable 503 on a keyed POST, not on a 307, and not after a GOAWAY (the write-safety fix's refusal raises its `TransportError`).
   - `Raw(factory)` and `Raw(b"...")` are re-sent whole on every attempt.
   - `Multipart` with a file that is not seekable is one-shot. With `BytesIO` or `bytes` it is re-sent with every part whole.
   - `Raw(...).replayable` and `Multipart(...).replayable` report each of these.
   Covered in Task 4.
6. **A walk's `query=` is the raw value on every page.** `BodyCursor` with a `write=` rewrites the value and the writer encodes each page. A `Cursor` without one sends the value as JSON. `query=None` walks GETs. A fixed idempotency key with `query=` is still refused. Covered in Task 5.

---

### Task 1: Branch, and commit this plan

**Files:**
- Create: `specs/2026-11-14-phase-3-per-call-options.md` (this plan).
- Modify: `specs/2026-11-05-public-api-redesign-design.md`:
  - 3.1: `stream` drops `max_response_bytes` too, and `CallOptions` lists it.
  - 3.3: `Multipart(files, fields)`, and `Raw` and `Multipart` state `replayable`.
  - Each is marked "(Amended with the phase 3 plan.)" as phase 2 did.

- [ ] **Step 1:** Work in `claude/api-redesign-phase-3-97b6ea`, rebased on `origin/main`.
- [ ] **Step 2:** Run `uv run typos specs/`. Expected: no output.
- [ ] **Step 3:** Commit: `Add the phase 3 plan of the API redesign`. Open it as a draft PR for review. `src/` stays untouched until the write-safety fix has merged.

### Task 2: `CallOptions`, and the per-call overrides move into it

**Files:**
- Modify: `src/headgate/_client.py`:
  - `_SendOptions`, `RequestOptions`, `_PerPage`, `PageOptions` and `_WalkOptions` become the bags in **Public signatures**, all of them built on one private base that is not closed. `body` stays on `RequestOptions` with today's five keys until Task 4.
  - Every verb, `head`'s overloads, `stream` and `paginate` lose their named `retry`, `follow_redirects` and `cache_ttl`.
  - `_Call` loses those three fields, and `_prepare`, `_retry_of` and the cache-TTL stamp read them from `call.options`, using `in` for absence.
  - `_retry_of`: `"retry" in options` gives `NO_RETRY` for `None`, or else the value. Next comes the view's retry when it set one (a `KEEP` default, as `deadline` has), then the route's.
  - The class docstring's **Per-call overrides** paragraph.
  - The new unknown-keyword check and the kind checks from **Decisions**, run first in each public entry point.
- Modify: `src/headgate/_view.py`:
  - `ViewOptions` is closed, with `retry: RetryPolicy | None` and `follow_redirects: bool`.
  - `ViewDefaults.retry` defaults to `KEEP`, and it gains `follow_redirects: bool | Literal[Keep.KEEP] = KEEP`. `layered` carries both.
  - `with_options` refuses `follow_redirects=True` under a `response_check`.
- Modify: `src/headgate/cache/_transport.py`: `stamp_ttl(request, ttl: float | None, ...)`, where `None` is stored as no lifetime limit.
- Create: `src/headgate/streaming/_options.py`, holding `StreamOptions`, exported from `headgate.streaming`.
- Modify: `src/headgate/__init__.py`: export `CallOptions`, drop `PageOptions`, and update the module docstring.
- Modify: every caller passing `retry=False`, `retry=True` or `follow_redirects=None` (`grep -rnE "retry=(True|False)|follow_redirects=None" src tests docs examples README.md`), and `tests/retry/test_properties.py`'s spellings only.
- Test: `tests/client/test_call_options.py`, `tests/client/test_retry_opt_out.py`, `tests/integration/test_views.py`, and `tests/client/test_call_options_types.py` (new, a ty subprocess test following `tests/urls/test_call_url_types.py`).

**Interfaces:**
- Produces: `CallOptions`, `RequestOptions`, `ViewOptions` and `StreamOptions` as in **Public signatures**, and the verb, `stream`, `paginate` and `with_options` signatures (`paginate` still takes `body=`/`json=` until Task 5).

- [ ] **Step 1: Write the failing tests:**
  - `test_retry_none_sends_once_and_absence_inherits` (Review Focus 1): on a view with `RetryPolicy(max_attempts=3)` over a 503, 503, 200 transport, `get(retry=None)` raises after one request, and `get()` returns after three.
  - `test_cache_ttl_none_stores_without_a_limit` (Review Focus 1), with `HttpCache(ttl=60)` and `TestClock`.
  - `test_a_view_follows_redirects_as_it_says`: `with_options(follow_redirects=False)` raises a 302 as its 3xx, and a call's `follow_redirects=True` on that view follows it.
  - `test_a_view_cannot_follow_redirects_under_a_response_check`.
  - `test_the_old_override_values_are_refused` (Review Focus 2): `retry=True`, `retry=False` and `follow_redirects=None`, given through `cast("Any", ...)` to each verb, to `stream`, to `paginate` and to `with_options`, each raise `TypeError` with the exact message, and record no request.
  - `test_an_unknown_keyword_is_refused`: `get(url, jsno=1)` raises `TypeError: get() got an unexpected keyword argument 'jsno'`, and records no request.
  - The ty test (Review Focus 3): the wrapper forwarding `CallOptions` gives exit code 0. Forwarding `RequestOptions` into `stream` gives exactly one error. `get(url, retry=True)` and `get(url, follow_redirects=None)` each give one `invalid-argument-type`.
- [ ] **Step 2:** Run them. Expected: FAIL.
- [ ] **Step 3:** Implement as in **Files**.
- [ ] **Step 4:** Run every gate and update the API snapshot. Expected diff: the verbs lose three named parameters, `PageOptions` goes, `CallOptions` and `headgate.streaming.StreamOptions` arrive, and `ViewOptions` changes.
- [ ] **Step 5:** CHANGELOG under **Changed**, with the two silent rows first, in bold, and the grep. The `cache_ttl=None` entry says that it removes headgate's lifetime and that the cache still ignores the response's cache headers, as the Migration section words it. Under **Removed**: `PageOptions`, and `retry=True`. Commit: `Move retry, follow_redirects and cache_ttl into the call options`.

### Task 3: `max_response_bytes` per call (#57)

**Files:**
- Modify: add the `MAX_RESPONSE_BYTES` slot, a `Slot[int]`, next to the stream `Slot` that the fix for the cache on the stream path puts on the call's context, wherever that fix lands it. That fix also edits `_body.py`'s `_rebuilt`, so rebase on it before starting this task.
- Modify: `src/headgate/_client.py`:
  - `CallOptions` gains `max_response_bytes: int`.
  - `_prepare` checks it and puts it on the context.
  - `_request` passes the call's cap, else the client's, to `read_capped`.
  - The class docstring's `ResponseTooLargeError` line says "past the call's `max_response_bytes`, else `HttpOptions.max_response_bytes`".
- Modify: `src/headgate/cache/_transport.py`: both `_buffer_up_to` calls use the request's slot when present, and `self._max_body_bytes` otherwise.
- Modify: `src/headgate/_body.py`: the comment at line 32 names the per-call option. Also `docs/configuration.md` and `docs/responses.md`, where the cap is described.
- Test: `tests/client/test_response_size_cap.py`, and `tests/cache/` for the cached cases.

- [ ] **Step 1: Write the failing tests** for each case in Review Focus 4, plus `max_response_bytes=0`, `-1`, `True` and `1.5`, each raising `TypeError` or `ValueError` with the `HttpOptions` wording.
- [ ] **Step 2:** Run them. Expected: FAIL. The keyword is unknown.
- [ ] **Step 3:** Implement as in **Files**.
- [ ] **Step 4:** Run every gate and update the snapshot (one new key in `CallOptions` and `RequestOptions`).
- [ ] **Step 5:** CHANGELOG under **Added**: one call may raise or lower the cap with `max_response_bytes=`, and a cached client buffers up to that call's cap. Reference #57, and close it from the PR body. Commit: `Let one call raise or lower the response-size cap`.

### Task 4: One `body=`

**Files:**
- Modify: `src/headgate/_request_body.py`:
  - add `Json`, `Form`, `Multipart` and `Raw` (final, frozen, slotted, each with its kind check)
  - `Body` becomes the union
  - `request_content` and `_Replayed` move behind `Raw`
  - `check_one_body` is deleted
  - the module docstring.
- Modify: `src/headgate/_client.py`:
  - `RequestOptions.body: Body | None` replaces the five keys.
  - `_prepare` matches the body once: `EncodedBody` gives content and `Content-Type`, `Json` gives `json=`, `Form` gives `data=`, `Multipart` gives `files=` with `data=`, and `Raw` gives `content=`. `REPLAYABLE_BODY` comes from `.replayable`.
  - `_prepare` stops calling `files_replayable` itself and reads `Multipart.replayable`, which calls it.
  - The `RequestOptions` docstring.
- Modify: `src/headgate/__init__.py`: export `Body`, `Json`, `Form`, `Multipart` and `Raw`.
- Modify: `src/headgate/msgspec.py` and `src/headgate/pydantic.py`: the `to_builtins` docstrings say `body=Json(...)`.
- Modify: every caller. Find them with `uv run ty check`, which rejects every old keyword in typed code, and with `grep -rnE "\b(json|data|content|files)=" docs examples README.md`. The grep must leave out `httpx2.Response(content=...)` in tests, which stays.
- Modify: `docs/configuration.md` (or wherever request bodies are documented): one section, a table like the design's 3.3, and `replayable`.
- Test: `tests/client/test_request_body.py` (new), `tests/integration/test_replayable_body.py`, and `tests/client/test_call_options_types.py`.

- [ ] **Step 1: Write the failing tests:**
  - Each body class is sent with the right bytes and `Content-Type`.
  - `EncodedBody`'s `Content-Type` still yields to the call's `headers=`.
  - Every case of Review Focus 5.
  - The kind checks: `body=b"x"`, `body={"a": 1}`, `Raw("text")`, `Raw([b"x"])` and `Form([("a", 1)])`.
  - The removed keywords from Review Focus 2.
  - `test_the_old_body_checks_are_gone`: `check_one_body` is not an attribute of `headgate._request_body`.
  - The ty cases: `post(url, json={})` is an `unknown-argument`, `post(url, body=b"")` is an `invalid-argument-type`, and `post(url, body=Json({}))` type-checks.
- [ ] **Step 2:** Run them. Expected: FAIL.
- [ ] **Step 3:** Implement as in **Files**.
- [ ] **Step 4:** Run every gate and update the snapshot.
- [ ] **Step 5:** CHANGELOG: under **Changed**, the four body rows of the Migration table, and that `Body` names the union. Under **Added**: `Json`, `Form`, `Multipart`, `Raw` and `.replayable`. Commit: `Make the request body one keyword whose value says what it is`.

### Task 5: The walk's body is `query=`

**Files:**
- Modify: `src/headgate/_client.py`:
  - `paginate`'s three overloads become the two in **Public signatures**.
  - `_walk_body`, `_UNPAGEABLE_BODIES` and the two keyword-to-pager refusals of `_post_refusal` are deleted. The fixed-key refusal stays.
  - `fetch_page` posts `body=write(value)` when the pager has a `write`, and `body=Json(value)` when it has none.
  - The docstring.
- Modify: `src/headgate/pagination/_pager.py` and `docs/pagination.md`, wherever they say `body=` or `json=` about a walk.
- Test: `tests/pagination/test_body_paging.py` and `tests/pagination/test_paginate.py`.

- [ ] **Step 1: Write the failing tests:** every case in Review Focus 6, and `paginate(body=...)` and `paginate(json=...)` raising `TypeError` naming `query=` before `first()` runs.
- [ ] **Step 2:** Run them. Expected: FAIL.
- [ ] **Step 3:** Implement as in **Files**.
- [ ] **Step 4:** Run every gate and update the snapshot.
- [ ] **Step 5:** CHANGELOG under **Changed**: `paginate(body=)` and `paginate(json=)` are `paginate(query=)`. Commit: `Name a walk's body query=, so body= means one thing`.

### Task 6: Verify the phase and open its PR

- [ ] **Step 1:** Run every gate from a clean tree, and the docs build (`uv run --locked --only-group docs zensical build --strict --clean`). Expected: all pass.
- [ ] **Step 2:** Run `grep -rnE "\b(json|data|content|files)=|retry=(True|False)|PageOptions|check_one_body" src docs examples README.md`. Expected: only `httpx2` calls headgate makes itself (`build_request(json=...)` in `_prepare`), and `httpx2.Response(content=...)`.
- [ ] **Step 3:** Run `uv run python -m pytest benchmarks/test_client.py` on this branch and on `origin/main`. Expected: no regression from the keyword check.
- [ ] **Step 4:** Dispatch the whole-branch review. Fix what it finds, as separate commits.
- [ ] **Step 5:** Push and mark the PR ready, titled `Phase 3 of the API redesign: one set of per-call options`. The body lists the Migration table, links the design's section 3, and closes #57. Do not merge.
