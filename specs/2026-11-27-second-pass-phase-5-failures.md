# Second pass, phase 5: Failures where they are made, testing through the front door, docs that run — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every mistake the review found fails where it is written, with a message naming the option, what it takes and
what it got; an SDK's tests read its clients back and resolve names without touching the network; and a test runs or
type-checks the docs' samples, so a removed spelling cannot survive in them.

**Architecture:** Kind checks go where each value is given, through three new helpers in `headgate._core._kinds`
beside `check_kind` and `check_int`. A refusal that must not be read as a body of the wrong shape is a private
`TypeError` subclass the dispatch lets through. `fake_network` keeps its one context variable, which now holds a handle
the stack asks for the transport and the resolver, and the client tells of itself when built. The docs test turns each
page's marked blocks into one module, type-checks every page in one `ty` run, and executes the blocks marked to run.

**Tech Stack:** Python 3.14, httpx2 2.16, hishel 2, msgspec, pydantic, ty, ruff, pytest, syrupy; cattrs and orjson join
the dev group for the docs' codec.

**Spec:** `specs/2026-11-27-api-second-pass-design.md`, sections 5 (5.1–5.5), 6 (6.1–6.2) and 7, approved 2026-11-27.

## Global Constraints

- Python 3.14 or newer; httpx2 2.16 or newer; hishel 2.
- Branch: `claude/api-second-pass-5-failures`, stacked on `claude/api-second-pass-4-surface`. No push and no PR.
- Clean break: no deprecation aliases; never fix a finding by documenting it.
- Gates for every task: `uv run ruff check`, `uv run ruff format --check`, `uv run ty check`, `uv run typos`,
  `uv run python -m pytest --cov` at 100% line and branch coverage in one process, and
  `uv run zensical build --strict` once docs change. No `noqa`, `ty: ignore` or `pragma: no cover`.
- A public API change updates the snapshot (`--snapshot-update`), diff reviewed.
- Every exported name appears in `docs/`; a public module binds only its `__all__` and subpackages; every export's
  hints resolve at runtime; every public signature names only exported types (`tests/package/test_one_home.py`).
- Each user-visible change gets a `CHANGELOG.md` entry under `## [Unreleased]` saying what to write instead; a new
  refusal says what used to be accepted. An earlier Unreleased entry naming what changes is updated. Grep after every
  edit. No blank lines inside a list.
- `tests/retry/test_properties.py` changes spellings only.
- Every message names the option, what it takes, and what it got.

## Decisions

1. **Three helpers join `_core/_kinds.py`.** `check_number(option, value, wanted)` refuses anything but an `int` or a
   `float`, a `bool` included, as `check_int` does for an `int`. `check_text_map(option, value, wanted)` refuses
   anything but a `Mapping` whose keys and values are all `str`: a list of pairs, and `{"X-N": 1}`.
   `check_statuses(option, value, wanted)` refuses a collection that is not one of `int`s (a `bool` and a `"500"`
   included) with `TypeError`, and a status outside 100–599 with `ValueError`. Each raises in `check_kind`'s words,
   `<option>= takes <wanted> (got <what>)`, the option named by its keyword or field name.
2. **A number is never a `bool`** (5.1): every numeric field of a value a client is configured with goes through
   `check_number` or `check_int`: `Timeouts`' four phases, `Rate`, `RateBudget(max_in_flight=)`, `RetryPolicy`
   (`attempts` an `int`, `max_retry_after`, `min_attempt_time`), `Backoff`, `Hedge`, `RetryShare`, `Adaptive`,
   `BreakerPolicy`, `BreakerRegistry(max_hosts=)`, `HttpCache(ttl=)`, the pagers' `page_size` and `start`, and the
   testing kit's `DropMidBody`, `Respond`, `Slow` and `TestClock`. The value checks after them are unchanged.
3. **A call's options are checked in `check_overrides`**, which every verb, `stream`, `paginate` and `with_options`
   already run: `params` a `Mapping` with `str` keys, `headers` through `check_text_map`, `idempotency_key` a `str`
   (`None` too is refused: leave it out for none, since a present key fixes one for a walk). `HttpOptions(headers=)`
   and `Expect(headers=)` use `check_text_map`; `events(last_event_id=)` takes a `str` or `None`.
4. **`stream(method=)` takes one of `Method`'s seven upper-case names**; any other value, `"get"` and `"BREW"`
   included, is a `TypeError` naming the seven, as the spec's 5.1 says.
5. **Retry statuses are 200–599**: a `RetryPolicy` may retry a 202 a polling API answers while a job runs, but not a
   1xx, which httpx2 never hands back. (Changed with the phase 5 review, which overturned 100–599.) `HttpCache(statuses=)` checks kinds and range with the same helper before its 2xx rule.
6. **`Expect.on(status=)` takes an `int`** (`check_int`), so `True` and `"404"` are `TypeError`s naming `status=`.
7. **An observer needs a callable `notify`** (`check_observers`): `observers=[print]` and one whose method is
   misspelled are `TypeError`s naming the entry.
8. **A pager is checked twice**: each of the seven strategies refuses, when built, a `read` that is not a `Reader` or
   an `Expect` and a hook (`items`, `next`, `more`, `put`, `offset`, `total`, `write`) that cannot be called; and
   `paginate()` refuses a `pager=` without callable `first`, `page_items` and `next_page`, naming what is missing,
   before `read`. (Amends spec 5.1, which named only `paginate`'s pager.)
9. **`Json` encodes its value when built (5.2)**, and a call encodes it again as it is sent. A value JSON's encoder
   cannot take raises `TypeError` naming the type and pointing to a flavor's `writer(Type)` or `to_builtins(value)`;
   `NaN` and the infinities raise `ValueError` where the `Json` is built, not at the call. (Changed during
   implementation: the plan kept the bytes in a private field, but a dataclass field, even `init=False`, is in
   `dataclasses.fields()`, `asdict()` and the API snapshot, mechanism in the public surface; a JSON body is small, and
   a large one is a writer's `EncodedBody`.)
10. **`Reader` refuses a class** (`isinstance(read, type)`), pointing to `as_text` for `str` and to
    `Reader(lambda r: Cls(r))` for a class that really takes a response.
11. **`headgate.msgspec`'s `decoder()` walks `msgspec.inspect.type_info`** and refuses every `CustomType` but `object`
    (which msgspec decodes as any value): a pydantic model points to `headgate.pydantic.reader(Model)`, any other class
    to a `msgspec.Struct`, a dataclass or a TypedDict. `headgate.pydantic` turns pydantic's
    `PydanticSchemaGenerationError` into a `TypeError` naming the class, so both flavors refuse the same way, and the
    `Codec` contract is that `decoder()` raises `TypeError` for a type it cannot decode. `check_codec` asks `decoder()`
    for a private plain class and fails unless that raises `TypeError`. (Changed during implementation: the plan
    accepted any exception, which needs a blind `except Exception`; one contract is also the more elegant API.)
12. **A misused `refuse=` is a private `ReaderMisuseError(TypeError)`** in `headgate._responses`, raised by the body
    reader when `refuse` returns neither a `RefusedError` nor `None`, and re-raised as it is by `Client._dispatch` and
    `mint_error` ahead of the shape errors. `TypeError` stays a shape error otherwise. `RefusedError(kind=)` is
    checked with `issubclass(kind, UpstreamStatusError)` when built. A `response_check` that returns neither is the
    same misuse, a `TypeError` naming `response_check=` (an adjacent gap, closed here; amends spec 5.3).
13. **A declared redirect on a following call (5.4)** is refused in `Client._request`, and for a walk in `paginate()`
    itself: when the call follows redirects and its `Expect` declares 301, 302, 303, 307 or 308, `ValueError` names the
    statuses and says to pass `follow_redirects=False`. A redirect that would send a one-shot body again (a 307 or 308,
    or a 301 or 302 that keeps the method) is not followed, so it reaches the call and may be declared:
    `_redirects.redirect_method(method, status)` says which. A stream has no `Expect`. (Changed during
    implementation: the existing `test_a_declared_redirect_answer_reads_the_unfollowed_hop` pins that case.)
14. **Auth values (5.1, 5.4):** `BearerAuth(token)` takes a `str` (`TypeError`) that is not empty or blank and does not
    start with `Bearer ` in any case (`ValueError`). `StaticHeaderAuth` and `QueryParamAuth` take a `check_text_map`
    mapping (`TypeError`) with at least one entry (`ValueError` saying to pass `auth=None`). The docs' "an empty
    mapping is a no-op" goes. `refresh_margin` was refused in phase 2.
15. **`cache_ttl=` on a client without a cache** is a `ValueError` at the call, in `_prepare`.
16. **The loop guard runs for every pager (5.5):** `PageWalk` remembers each page's body when `pager.page_items(page)`
    yields an item (read with `next(iter(...), sentinel)`, so an iterator is not consumed for the walk), and the
    `isinstance` on `Offset`, `PageNumber` and `BodyOffset` goes. `test_link_header_pages_with_the_same_body_are_walked`
    pinned the old exemption and becomes its opposite; three tests of the repeated-request check get pages that
    differ, since the body check now catches their loop one page sooner.
17. **`fake_network` yields a `FakeNetwork[T]` (6.1)**, a final class exported by `headgate.testing`, with `transport`
    (the `T` given) and `clients`, a tuple read afresh, of the `Client`s whose construction finished in the block, in
    order, and a `repr`. A view (`with_options`) is not a new client. `NETWORK_OVERRIDE` holds a private
    `NetworkOverride` dataclass of `_stack.py` (`transport`, `resolver`, `clients`), which `Client.__init__` appends
    itself to as its last step; a handle finds its own through a private weak map, as a `RateBudget` its pacer, so the
    handle shows only what a test reads. (Amends spec 6.1, which named no type. Changed during implementation: the plan
    made the override a protocol with `resolve` and `built` methods, which would have put the stack's mechanism on
    the public handle.)
18. **Names resolve inside the fake network (6.2):** under `fake_network`, the SSRF guard resolves through the handle,
    replacing even an SDK's own `resolver=`, as the transport replaces an SDK's own. A host resolves to
    `FakeTransport.resolve(host, *addresses)`'s answer (case-insensitive; no addresses is a name that does not resolve,
    a `TransportError`), and every other name, and every name over a transport that is not a `FakeTransport`, to
    `1.2.3.4`, a globally routable address the guard admits. The check keeps running, so a private literal or a
    `resolve("api.example", "10.0.0.1")` is refused. (Amends spec 6.2 with the override and the empty answer.)
19. **The docs test's marker is an HTML comment on the line before the fence**: `<!-- docs-test: run -->` (type-check
    and execute) or `<!-- docs-test: check -->` (type-check only, for a block that calls a server). It renders as
    nothing on GitHub, PyPI and the site. Each page's marked blocks, in order, form one module after
    `from headgate import *` and `from tests.examples.doc_context import *` (the domain types, values and SDKs a block
    names without defining, all real); a block's statements from its first top-level `await` on are wrapped in an
    `async def`, so the imports and definitions before it stay at the top for the page's later blocks, and each line
    maps back to `page:line`. One `ty check` (every rule an error) runs over every page's module. For the run, the
    page's module is written out without the awaiting tails of its check blocks and imported, in a temporary directory,
    inside `fake_network(FakeTransport())` and `TestClock().install()`; then every argument-free wrapped block and
    `test_*` function it defined is called (awaited when async), so the codec guide's `check_codec` test and the
    testing guide's budget test run. The context's own clients are built on a fake network too, so a block run by
    mistake raises rather than reaching the internet. A run is an import, not `exec`, which the lint rules refuse.
20. **The README is covered by its excerpt test**: each README block is, statement by statement, code of a module in
    `examples/` or `tests/examples/` that the suite runs and ty checks, which is stronger than running the block on its
    own; its blocks are not marked. (Amends spec 7, which named the README among the marked pages.)
21. **cattrs and orjson join the dev group** so the codec guide's example (E5) runs through `check_codec`; the core
    and the extras do not change.

## Review Focus

1. **A `bool` where a status goes** (`Expect.on(True, ...)`, `RetryPolicy(statuses={True})`): a `TypeError`, not
   status 1. Tested in Task 3.
2. **A `Json` of a tuple, or of a dict with `int` keys**, is still accepted as JSON's encoder takes it, and two equal
   `Json`s stay equal and hash alike. Tested in Task 5.
3. **Under `fake_network`, a host resolved to a private address, or to nothing**, is refused (`ForbiddenDestination
   Error`) or fails as a `TransportError`, before the fake is sent anything. Tested in Task 9.
4. **A pager whose `page_items` returns an iterator** still yields every item of each page after the loop guard
   looked at it. Tested in Task 8.
5. **A view's `headers=[("X", "1")]`** is refused by `with_options`, not at its first call. Tested in Task 2.

---

### Task 1: Commit the plan

- [ ] `uv run typos specs/`; mark spec 5.1, 5.3, 6.1, 6.2 and 7 "(Amended with the phase 5 plan.)" per Decisions 8, 12,
  17, 18 and 20; commit both.

### Task 2: A call's options are checked for their kind (5.1, calls)

**Files:** `_core/_kinds.py`, `_core/__init__.py`, `_call_options.py`, `_client.py` (`stream`, `events`),
`_options.py` (`HttpOptions`), `_responses.py` (`Expect(headers=)`); tests `tests/client/test_option_kinds.py` (new).

**Interfaces:** `check_number(option: str, value: object, wanted: str) -> None`;
`check_text_map(option: str, value: object, wanted: str) -> None`;
`check_statuses(option: str, value: object, wanted: str) -> None`.

- [ ] **Tests** (each `pytest.raises(TypeError, match=...)` naming the option): `params=[("a", 1)]`;
  `headers=[("X", "1")]` and `{"X-N": 1}` on a call, on `with_options` (raised by `with_options`) and on
  `HttpOptions`; `idempotency_key=123` and `None`; `events(last_event_id=5)`; `stream(method="BREW")` and `"get"`;
  `Expect(nothing, headers={"X": 1})`. Run: fail.
- [ ] Implement Decisions 1, 3 and 4. Changelog **Changed** (what each used to do: an `AttributeError`, httpx2's own
  words, or sent as given). Commit.

### Task 3: Values a client is configured with are checked for their kind (5.1, values)

**Files:** `_options.py` (`Timeouts`), `ratelimit/_rate.py`, `ratelimit/_budget.py`, `retry/_policy.py`,
`breaker/_policy.py`, `breaker/_registry.py`, `cache/_policy.py`, `_responses.py` (`Expect.on`), `_options.py`
(`check_observers`), `pagination/_pager.py`, `testing/_faults.py`, `testing/_time.py`, `_client.py` (`_page_reader`);
tests `tests/core/test_value_kinds.py` (new), beside the pager and observer tests.

- [ ] **Tests:** a parametrized `test_a_number_is_never_a_bool_or_text` over every field of Decision 2 with `True` and
  `"5"` (and `2.5` for an `int` field), each a `TypeError` naming the field; `RateBudget(5)`; `RetryPolicy(statuses=
  frozenset({"500"}))`, `{True}` (`TypeError`) and `{999}` (`ValueError`); `HttpCache(statuses=("200",))`;
  `Expect(as_json).on("404", nothing)` and `.on(True, nothing)`; `Client(observers=[print])` and an observer with
  a misspelled `notify`; `Cursor(5, items=..., ...)`, `LinkHeader(reader, items=5)`; `paginate(pager=None)` and a pager without
  `first`. Run: fail.
- [ ] Implement Decisions 2 and 5–8. Snapshot if a signature changes. Changelog **Changed**. Commit.

### Task 4: Auth values fail where they are built (5.1, 5.4 auth)

**Files:** `_auth.py`; docs `auth.md`; tests `tests/auth/test_header_and_query_auth.py`,
`tests/auth/test_credential_stampers.py`, `tests/auth/test_auth_base.py`.

- [ ] **Tests:** `BearerAuth("")`, `"  "`, `"Bearer abc"`, `"bearer abc"` (`ValueError`), `BearerAuth(5)`
  (`TypeError`); `StaticHeaderAuth({})`, `QueryParamAuth({})` (`ValueError` naming `auth=None`); `QueryParamAuth("key")`,
  `StaticHeaderAuth([("X", "1")])`, `{"X": 1}` (`TypeError`). Run: fail.
- [ ] Implement Decision 14; tests that built an empty auth build one with a name. Changelog. Commit.

### Task 5: `Json` encodes its value when built (5.2)

**Files:** `_request_body.py`, `_client.py` (`_prepare`'s docstring); tests `tests/client/test_bodies.py` (or the body
test module that exists).

- [ ] **Tests:** `Json(Order(1))` and `Json({"due": date(2026, 1, 1)})` raise `TypeError` naming the type, `writer(`
  and `to_builtins`; `Json(float("nan"))` raises `ValueError` where built; `Json((1, 2))` and `Json({1: "a"})` build and
  send `[1,2]` and `{"1":"a"}`; `Json({"a": 1}) == Json({"a": 1})` and their hashes agree for a hashable value. Run:
  fail.
- [ ] Implement Decision 9. Changelog. Commit.

### Task 6: Readers fail where they are built (5.3)

**Files:** `_responses.py`, `_errors.py` (`RefusedError`), `_msgspec.py`, `testing/_codec.py`, `_client.py`
(`_dispatch`, `_checked`), `_failures.py` (`mint_error`); docs `responses.md`, `codecs.md`; tests
`tests/decoding/`, `tests/testing/test_check_codec.py`.

**Interfaces:** `class ReaderMisuseError(TypeError)` in `headgate._responses` (private module, not exported).

- [ ] **Tests:** `Reader(str)` and `Reader(int)` raise `TypeError` naming `as_text`; `@Reader` over a `def` and a
  callable instance still build; `headgate.msgspec.reader(PydanticModel)` names `headgate.pydantic.reader`,
  `reader(Plain)`, `reader(list[Plain])` and `reader(dict[str, Plain] | None)` name the class, `reader(object)` builds;
  `check_codec` on a codec whose decoder accepts a plain class fails naming it; a reader whose `refuse` returns
  `"refused"` raises `TypeError` naming `refuse=` through a call, not `ResponseParseError`; `RefusedError("x",
  kind=ValueError)` raises `TypeError`; a `response_check` returning `"no"` raises `TypeError` naming
  `response_check=`. Run: fail.
- [ ] Implement Decisions 10–12. Changelog. Commit.

### Task 7: Declarations that cannot happen (5.4)

**Files:** `_client.py` (`_request`, `_prepare`); docs `responses.md`, `caching.md`; tests
`tests/client/test_declared_redirects.py` (new), the cache TTL tests.

- [ ] **Tests:** `Expect(nothing).on(302, nothing)` on a following call raises `ValueError` naming 302 and
  `follow_redirects=False`, and reads the 302 with `follow_redirects=False`; on a page walk too; `cache_ttl=5` on a
  client without a cache raises `ValueError` naming `cache_ttl=`. Run: fail.
- [ ] Implement Decisions 13 and 15. Changelog. Commit.

### Task 8: Every pager gets the loop guard (5.5)

**Files:** `pagination/_paginated.py`; docs `pagination.md`; tests `tests/pagination/`.

- [ ] **Tests:** a custom `Pager` (an offset of its own) over a server that ignores the offset raises `ClientError`
  ("repeats an earlier page's body") after two requests; a pager whose `page_items` returns an iterator walks every
  item; a page with no items that repeats is not a loop. Run: fail.
- [ ] Implement Decision 16. Changelog. Commit.

### Task 9: `fake_network` reports what it built and resolves names (6.1, 6.2)

**Files:** `_stack.py`, `_client.py` (`__init__`), `testing/_transport.py`, `testing/__init__.py`; docs `testing.md`,
`security.md`, README's testing paragraph if it names the yield; tests `tests/testing/test_fake_network.py` and
`tests/examples/*` that use the yielded value.

**Interfaces:** `class FakeNetwork[T: httpx2.AsyncBaseTransport]` with `transport: T` and `clients: tuple[Client,
...]`; `fake_network[T](transport: T) -> Generator[FakeNetwork[T]]`; `FakeTransport.resolve(host: str, *addresses:
str) -> Self`; `NetworkOverride` protocol in `_stack.py`.

- [ ] **Tests:** `with fake_network(fake) as net:` yields `net.transport is fake` and `net.clients == (sdk.http,)` for
  two SDKs built in the block, a view not among them, a client whose construction raised not among them;
  `budget_for(net.clients[0])` reads the SDK's rate; a guarded SDK on `api.example` (and with its own `resolver=`)
  sends to the fake with no DNS; `fake.resolve("api.example", "10.0.0.1")` is refused with
  `ForbiddenDestinationError` and nothing sent; `fake.resolve("api.example")` raises `TransportError`; a private
  literal is still refused. Run: fail.
- [ ] Implement Decisions 17 and 18. Snapshot. Changelog **Changed** (`with fake_network(fake) as net:` yields the
  handle, not the transport: write `net.transport`) and **Added** (`FakeNetwork`, `FakeTransport.resolve`). Commit.

### Task 10: Docs that run (7)

**Files:** `docs/*.md` (markers, drifts), `README.md` (drifts), `tests/examples/doc_context.py` (new),
`tests/examples/test_doc_samples.py` (new), `pyproject.toml`/`uv.lock` (cattrs, orjson), `src/headgate/_codec.py`
(`Codec` docstring, E15).

- [ ] Fix the drifts F10 and E19 list (verbs with `query`; `json=` on a walk; `events()` in the README; `sent` and
  `source` on every error; one SDK boilerplate, `aclose()` forwarding, on `index.md`; the path escape hatches in the
  README and the per-call options; `auth.md`'s "once per call" scoped to the static auths; `if token is not None`;
  `HttpOptions(transport=)` is the network only, behavior belongs in middleware), the lambda pager on `index.md`
  (3.2), the cattrs codec (strict hooks, `TypeForm`, refusing an unknown class at `decoder()`), and the `Codec`
  docs' built-in containers (E15). Commit.
- [ ] **Tests:** `test_every_marked_page_type_checks` (one `ty` run, failures mapped to `page:line`),
  `test_every_run_block_runs[page]`, `test_the_markers_are_known` (a marker other than `run` or `check`, or one not
  followed by a Python fence, fails). Mark every block that can run; record each unmarked block's reason below.
  Commit.

### Task 11: Gates and report

- [ ] Every gate's last line green; snapshot diff reviewed; every changelog edit grepped; `git grep` for each removed
  spelling in `docs/`, `README.md`, `examples/`, `benchmarks/` and messages.

## Unmarked blocks

None. Of the guides' 61 Python blocks, 46 are marked `run` and 15 `check`; the 6 README blocks are covered by the
excerpt test (Decision 20). Every block that calls a server is `check`: the per-call options, a view's call and the
wrapper's calls in `configuration.md`; the deadline calls and the bounded stream in `deadlines.md`; the error handler
in `errors.md`; the SDK in use on `index.md`; the walks in `pagination.md` (its first, `collect()`, the page budget,
the body cursor); the delete in `security.md`; every block of `streaming.md`. Four blocks that could not run as they
stood were rewritten to run or check rather than left unmarked: the request-body fragment of `streaming.md` (a method
body with `self` and `...`), the wrapper method of `configuration.md` (now a small class), the `HeaderTokens` and
`SearchPages` pagers (a truthiness test of `str | None`, and a page type that clashed with the page's other one).
