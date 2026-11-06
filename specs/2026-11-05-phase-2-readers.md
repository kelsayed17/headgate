# Phase 2: Readers — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a reader a real type, so a wrong `expect=` is a type error and a runtime `TypeError`. Give readers and writers one vocabulary, and split the two kinds of refusal check by what each sees.

**Architecture:** `Reader[T]` becomes a final generic class wrapping a function of the response. Every reader headgate builds or ships is an instance, and `expect=` and `Expect` accept nothing else. The builders are renamed to one pair, `reader_for` and `writer_for`, which the flavor modules mirror as `reader` and `writer`. The host check becomes `HttpOptions(response_check=)`, the reader's becomes `refuse=`, and `RefusedError` moves to `headgate.errors`.

**Tech Stack:** Python 3.14, httpx2 2.16, ty, ruff, pytest, syrupy (API snapshot).

**Spec:** `specs/2026-11-05-public-api-redesign-design.md`, section 2 (Readers). Phase 1 (section 1) merged as `afaea04`.

## Global Constraints

- Python 3.14 or newer; httpx2 2.16 or newer; hishel 2.
- Base branch: `origin/main` at or after `afaea04`.
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
- Each task adds a `CHANGELOG.md` entry under `## [Unreleased]`, in **Changed** or **Removed**, saying what to write instead.
- Docstrings describe what the code does now, in the present tense.
- The property tests in `tests/retry/test_properties.py` keep their properties and assertions.
- Out of scope: the top-level trim (phase 8), `body=` and `CallOptions` (phase 3). A name this phase renames stays where it is exported today unless this plan moves it.

## Decisions this plan makes

The spec leaves these to the builder, or does not settle them. Each is the planner's call, made by the maintainer's rule: the cleanest design, even where it breaks things.

- **`Expect` stays not callable.** A callable `Expect` would be a second way to read a response, outside the client's dispatch. Its docstring gives that reason in place of today's ("the union is unambiguous only because of it").
- **The runtime refuses a non-reader as well as the type checker.** `expect=`, `Expect(ok)`, the values in `Expect(answers=)` and `Expect.on` raise `TypeError` for anything that is not a `Reader` (or, for `expect=`, an `Expect`). `str` and a model class are both callable, so only an `isinstance` check catches them at runtime.
- **`Reader(read, /, *, name=None)`.** The `repr` is `Reader(<name>)`. With no name given, the name is the function's `__qualname__`, or its `repr` if it has none (a `functools.partial`). `reader_for` names the codec's class and the type expression, so `Expect(...)` prints what it reads. The exact format is up to the implementer.
- **`JSON` is a `Reader[object]`**, the honest type of `json.loads`. Adding a JSON value alias would add a name.
- **`RefusalCheck` becomes `ResponseCheck`**, exported where `RefusalCheck` is today. Phase 8 decides its final home.
- **`RefusedError` leaves the top level** and lives only in `headgate.errors`, as spec 2.3 says.
- **`body_reader` keeps `empty: bytes | None`.** It has no codec, so it cannot encode the empty array itself. Its other keyword is renamed `refuse=`, like the other builders.
- **`decode_with` and the flavors' `decode` stay**: they decode bytes, not responses.
- **`Writer[T]` stays a type alias** of a function returning `EncodedBody`. Only a pager's `write=` receives one, and a plain function is the hand-written writer.

## Review Focus

These five cases are not covered by the spec's own examples and are the most likely to bite users. Each line gets a test in the task named.

1. **Every callable that is not a `Reader`**, given as `expect=` to every verb, `stream`, `paginate`, `Expect(ok)`, an `answers` value, `Expect.on` or `headgate.cache.conditional`, raises `TypeError` naming the option and the type it got. That includes `str`, a model class, a plain function and a lambda. Covered in Task 2.
2. **Variance:** a `Reader[Order]` type-checks where a `Reader[Order | None]` is expected, so `Expect(ORDER).on(404, nothing)` still widens. ty infers the variance from the class body, so a mutable attribute typed `T` would silently make it invariant. Covered in Task 2.
3. **Readers headgate builds internally** are `Reader`s and still work: `conditional()`'s reader, `paginate`'s page reader (`_paired`), and `stream`'s default `expect=nothing`. Covered in Task 2.
4. **`JSON` on a bad body**: an empty body, invalid JSON or non-UTF-8 bytes on a 2xx raises `ResponseParseError`, not `json.JSONDecodeError` or `UnicodeDecodeError`. On a declared non-2xx it falls through to the status ladder. Covered in Task 3.
5. **The old spellings fail loudly.** `HttpOptions(refusal_check=...)` raises `TypeError` (an unknown keyword). `reader_for(..., refusal_check=...)` does not type-check. Importing `RefusedError`, `json_reader`, `json_writer`, `decoder_for` or `encoder_for` from `headgate` raises `ImportError`. Covered in Tasks 3 and 4.

---

### Task 1: Branch, and commit this plan

**Files:**
- Create: `specs/2026-11-05-phase-2-readers.md` (this plan).

**Interfaces:**
- Produces: the branch `claude/api-redesign-phase-2`, cut from `origin/main`, in its own worktree.

- [ ] **Step 1:** Create the worktree and branch from `origin/main`. Check that `git log origin/main` contains `afaea04`.
- [ ] **Step 2:** Copy this plan into `specs/`. Run `uv run typos specs/`. Expected: no output.
- [ ] **Step 3:** Commit: `git add specs && git commit -m "Add the phase 2 plan of the API redesign"`.

### Task 2: `Reader[T]` is a final class, and `expect=` takes only a `Reader` or an `Expect`

**Files:**
- Modify: `src/headgate/_responses.py`:
  - the `Reader` alias becomes the class
  - `nothing`, `as_bytes` and `event_stream` become module-level `Reader` instances. Their docstrings become `#:` comments, or the functions stay private and the instances wrap them.
  - `body_reader` and `json_reader` return `Reader`. `json_reader` is removed in Task 3.
  - `Expect.__post_init__` and `Expect.on` check that every reader is a `Reader`
  - the `Expect` docstring gives the new reason it is not callable.
- Modify: `src/headgate/_codec.py`: `decoder_for` returns a `Reader` named after the codec and the type.
- Modify: `src/headgate/_client.py`:
  - the two `spec: Expect[...] = expect if isinstance(expect, Expect) else Expect(expect)` lines (around lines 656 and 704) become one helper that raises `TypeError` for a value that is neither
  - `_paired` returns `Reader(...)`
  - `stream`'s `expect=` checks its value the same way.
- Modify: `src/headgate/cache/_conditional.py`: `conditional()` checks that its `reader` is a `Reader`, and builds its `fresh` reader as one.
- Modify: every hand-written reader passed as `expect=` or to `Expect`, wrapped in `Reader(...)`. That covers the tests (`tests/support/responses.py`, `tests/cache/_helpers.py`, `tests/client/test_error_messages.py`, `tests/client/test_client.py`, `tests/cache/test_store_on_accept.py`, `tests/integration/test_replayable_body.py`, `tests/security/test_redirect_hardening.py`, `tests/observe/test_accept.py`, `tests/streaming/test_stream.py`), plus `examples/`, `README.md` and `docs/`. Find them with `ty check`, which now rejects each one.
- Modify: `docs/responses.md`, the section that defines a reader: a reader is `Reader(fn)`. Show `repr` in one example, and the decorator form (`@Reader` over a `def`), which needs no extra support since `Reader(fn)` takes the function positionally.
- Test:
  - `tests/decoding/test_reader.py` (new)
  - `tests/decoding/test_reader_types.py` (new, a ty subprocess test following `tests/urls/test_call_url_types.py`)
  - `tests/cache/test_conditional.py` and `tests/pagination/test_paginate.py`, for Review Focus 3.

**Interfaces:**
- Produces: `headgate.Reader`, a `@final` generic class: `Reader[T](read: Callable[[httpx2.Response], T], /, *, name: str | None = None)`, with `__call__(self, response: httpx2.Response, /) -> T` and `__repr__`. It uses `__slots__`, and its attributes are private and read-only, so `T` is inferred covariant.
- Produces: `nothing: Reader[None]`, `as_bytes: Reader[bytes]` and `event_stream: Reader[list[httpx2.ServerSentEvent]]`, keeping the names they have today.
- Produces: every verb, `stream`, `paginate`, `Expect` and `Expect.on` typed `Reader[T] | Expect[T]` (or `Reader[T]`), as today, now over the class.

- [ ] **Step 1: Write the failing tests** in `tests/decoding/test_reader.py`:
  - `test_a_reader_calls_its_function_with_the_response`: `Reader(lambda r: r.status_code)(httpx2.Response(201))` is `201`.
  - `test_a_readers_repr_names_its_function`: given `def orders(r): ...`, `repr(Reader(orders))` is `"Reader(orders)"`. With `name="rows"` it is `"Reader(rows)"`. `repr(Reader(functools.partial(f, 1)))` does not raise and contains `"partial"`.
  - `test_a_codec_readers_repr_names_the_type`: `repr(decoder_for(CODEC, list[Order]))` contains `"Order"`. That test moves to `reader_for` in Task 3.
  - `test_stock_readers_are_readers`: `nothing`, `as_bytes` and `event_stream` are `Reader` instances. `repr(nothing) == "Reader(nothing)"`.
  - `test_expect_refuses_a_callable_that_is_not_a_reader`, parametrized over `str`, a class, a plain function and a lambda, given as `Expect(ok)`, as an `answers` value, to `.on(404, ...)` and to `conditional(...)`. Each raises `TypeError` whose message contains `"Reader"` and the type's name.
  - `test_every_call_refuses_a_non_reader_expect`, parametrized over `get`, `post`, `put`, `patch`, `delete`, `head`, `stream` and `paginate` on a `StubClient` with `expect=str`. Each raises `TypeError` naming `expect=` before anything is sent: the fake transport records no request.
- [ ] **Step 2: Write the failing type test** `tests/decoding/test_reader_types.py`:
  - `test_a_non_reader_expect_does_not_type_check`: `c.get("x", expect=str)`, `c.get("x", expect=Order)`, `c.get("x", expect=lambda r: r)` and `Expect(lambda r: r)` each produce an error. The exit code is non-zero, and the output has one `invalid-argument-type` per line (four).
  - `test_a_reader_widens_through_expect_on`: `e: Expect[Order | None] = Expect(ORDER).on(404, nothing)` and `r: Reader[Order | None] = ORDER` type-check with exit code 0 (Review Focus 2).
- [ ] **Step 3:** Run `uv run python -m pytest tests/decoding/test_reader.py tests/decoding/test_reader_types.py -v`. Expected: FAIL. `Reader` cannot be called as a class yet, and no `TypeError` is raised.
- [ ] **Step 4:** Implement the class and the checks as in **Files**. The runtime check message follows the project's style, for example `expect= takes a Reader or an Expect (got type)`.
- [ ] **Step 5:** Run `uv run ty check`, and wrap every hand-written reader it reports in `Reader(...)` across tests, examples, `README.md` and `docs/`. Add Review Focus 3 tests:
  - in `tests/cache/test_conditional.py`, a conditional read returns `Fresh` and then `NotModified`
  - in `tests/pagination/test_paginate.py`, a two-page walk whose pager reads a header from the response.
  Both existing tests may already cover this: confirm each fails if `_paired` or `fresh` returns a bare function, and add an assertion if not.
- [ ] **Step 6:** Run every gate, and update the API snapshot. Expected: all pass. In the snapshot diff, `Reader` changes from a type alias to a class, and `nothing`, `as_bytes` and `event_stream` change from functions to `Reader` values.
- [ ] **Step 7:** Add the CHANGELOG entry under **Changed**: a reader is now `Reader(fn)`, and `expect=` refuses anything else. Commit: `Make Reader a class, so a wrong expect= fails`.

### Task 3: One vocabulary for readers and writers

**Files:**
- Modify: `src/headgate/_codec.py`:
  - `decoder_for` becomes `reader_for(codec, type_, *, allow_empty_body=False, refuse=None) -> Reader[T]`
  - `encoder_for` becomes `writer_for(codec, type_) -> Writer[T]`
  - the module docstring.
- Modify: `src/headgate/_responses.py`:
  - `body_reader(decode, /, *, empty=None, refuse=None)`
  - remove `json_reader`
  - add `JSON`
  - the module docstring, which today names `json_reader`.
- Modify: `src/headgate/_request_body.py`: remove `json_writer`. `EncodedBody` defaults its `content_type` to `"application/json"`, which stays.
- Modify: `src/headgate/msgspec.py` and `src/headgate/pydantic.py`:
  - `decoder` becomes `reader(type_, *, strict=True, allow_empty_body=False, refuse=None)`
  - `encoder` becomes `writer(type_, *, drop_none=False)`
  - the `__all__`, docstrings and the `to_builtins` docstrings that mention `json=` or `data=` keep their wording (phase 3 changes those).
- Modify: `src/headgate/__init__.py`:
  - export `reader_for`, `writer_for` and `JSON`
  - drop `decoder_for`, `encoder_for`, `json_reader` and `json_writer`
  - the module docstring.
- Modify: every use across `tests/`, `examples/`, `README.md` and `docs/`, for example `docs/codecs.md`, `docs/responses.md` and `examples/series_client.py`. Find them with `grep -rnE "decoder_for|encoder_for|json_reader|json_writer|\b(decoder|encoder)\(|refusal_check=" src tests docs examples README.md`. Leave `Codec.decoder`, `Codec.encoder` and `HttpOptions(refusal_check=)`, which Task 4 renames.
- Test: `tests/decoding/test_codecs.py`, `tests/decoding/test_msgspec.py`, `tests/decoding/test_pydantic.py`, `tests/decoding/test_responses.py`, `tests/decoding/test_reader.py`.

**Interfaces:**
- Consumes: `Reader` from Task 2.
- Produces:
  - `reader_for[T](codec: Codec, type_: TypeForm[T], *, allow_empty_body: bool = False, refuse: Callable[[T], RefusedError | None] | None = None) -> Reader[T]`
  - `writer_for[T](codec: Codec, type_: TypeForm[T]) -> Writer[T]`
  - `body_reader[T](decode: Callable[[bytes], T], /, *, empty: bytes | None = None, refuse: Callable[[T], RefusedError | None] | None = None) -> Reader[T]`
  - `JSON: Final[Reader[object]]`, named `"JSON"`. It decodes the body with `json.loads` and raises `DecodeError` for a body that is not JSON, naming the position and never the input, with the library's error not chained.
  - `headgate.msgspec.reader`, `headgate.msgspec.writer`, `headgate.pydantic.reader` and `headgate.pydantic.writer`.

- [ ] **Step 1: Write the failing tests:**
  - In `tests/decoding/test_responses.py`:
    - `test_json_reads_builtins_without_a_codec`: `JSON(httpx2.Response(200, content=b'{"a": [1, null]}'))` is `{"a": [1, None]}`.
    - `test_json_refuses_a_body_that_is_not_json` (Review Focus 4): through a `StubClient`, each of `b""`, `b"{"` and `b"\xff"` on a 200 raises `ResponseParseError` whose message holds no byte of the body and whose `__cause__` is `None`. Called directly, `JSON` raises `DecodeError` for each of them. The same bodies on a 404 declared with `Expect(nothing).on(404, JSON)` raise `UpstreamStatusError` with `status_code == 404`.
    - `test_a_body_reader_refuses_with_refuse`: rename today's `test_a_body_reader_raises_the_refused_its_check_returns` and pass `refuse=`.
  - In `tests/decoding/test_codecs.py`, rename the `decoder_for` and `encoder_for` tests to `reader_for` and `writer_for`, using `refuse=`.
  - In the msgspec and pydantic tests, rename the `decoder` and `encoder` tests to `reader` and `writer`.
  - In `tests/decoding/test_reader.py`, `test_the_old_builders_are_gone` (Review Focus 5): each of `json_reader`, `json_writer`, `decoder_for` and `encoder_for` is missing from `headgate`, and `decoder` and `encoder` are missing from `headgate.msgspec` and `headgate.pydantic`, checked with `hasattr`. (`RefusalCheck` and `RefusedError` move in Task 4, which extends this test.)
- [ ] **Step 2:** Run `uv run python -m pytest tests/decoding -v`. Expected: FAIL with `ImportError` or `AttributeError` on the new names.
- [ ] **Step 3:** Implement the renames and `JSON`. Build `JSON` on `body_reader`, with a decode that catches `ValueError` (which `JSONDecodeError` and `UnicodeDecodeError` both are) and raises `DecodeError` from `None`. The message is `not JSON at line L, column C` for a `JSONDecodeError`, and `not UTF-8 at byte N` for a `UnicodeDecodeError`. `UnicodeDecodeError`'s own message quotes the byte, so it is never used.
- [ ] **Step 4:** Update every use found by the grep in **Files**. Rewrite `docs/codecs.md`'s list of builders, and `docs/responses.md`'s paragraph on `json_reader` ("`json_reader(decode, allow_empty_body=True)` is `body_reader(decode, empty=b"[]")`"), which now shows `body_reader` alone.
- [ ] **Step 5:** Run every gate, and update the API snapshot. Expected: all pass. In the snapshot diff, `json_reader`, `json_writer`, `decoder_for` and `encoder_for` are removed, `JSON`, `reader_for` and `writer_for` are added, and the flavor modules show `reader` and `writer`.
- [ ] **Step 6:** Add the CHANGELOG entries, each with its replacement:
  - under **Changed**: `decoder_for` → `reader_for`, `encoder_for` → `writer_for`, `msgspec.decoder`/`encoder` → `msgspec.reader`/`writer` (and pydantic's), and `refusal_check=` on a reader → `refuse=`
  - under **Removed**: `json_reader` (use `body_reader(decode, empty=b"[]")`) and `json_writer` (write a function returning `EncodedBody`)
  - under **Added**: `JSON`.
  Commit: `Name the reader and writer builders reader_for and writer_for`.

### Task 4: The response check is `response_check=`, and `RefusedError` lives in `headgate.errors`

**Files:**
- Modify: `src/headgate/_options.py`:
  - `HttpOptions.refusal_check` becomes `response_check: ResponseCheck | None = None`
  - its docstring bullet
  - the `follow_redirects` refusal message: `an HttpOptions with a response_check= must set follow_redirects=False`.
- Modify: `src/headgate/_responses.py`:
  - `RefusalCheck` becomes `ResponseCheck`, with the same definition
  - `RefusedError` moves out
  - `__all__`.
- Modify: `src/headgate/errors.py`: `RefusedError` moves here unchanged. It is still not a `ClientError`, so an `except ClientError` does not catch it. Add it to `__all__` and to the module docstring's tree, as a sibling noted "converted by the client".
- Modify: `src/headgate/_client.py`:
  - `self._refusal_check` and `_refusal` become `_response_check`
  - the per-call message `... has a response_check=, so no call may follow redirects`
  - every docstring that names `refusal_check` (find them with `grep -n refusal_check src/headgate/_client.py`; line numbers drift after Task 2).
- Modify: imports of `RefusedError` in `src/headgate/_failures.py`, `src/headgate/_codec.py`, `src/headgate/msgspec.py`, `src/headgate/pydantic.py` and `src/headgate/streaming/_stream.py`, and `refusal_check` in that file's docstring.
- Modify: `src/headgate/__init__.py`: export `ResponseCheck` in place of `RefusalCheck`, and drop `RefusedError`.
- Modify: every use in `tests/`, `docs/` (`configuration.md`, `responses.md`, `caching.md`, `design.md` and `errors.md`) and `examples/`. Find them with `grep -rn "refusal_check\|RefusalCheck\|from headgate import.*RefusedError" src tests docs examples README.md`.
- Test:
  - `tests/client/test_client.py` and `tests/errors/test_error_messages_snapshot.py`, whose host-check tests are renamed
  - `tests/decoding/test_reader.py` for Review Focus 5.

**Interfaces:**
- Consumes: `refuse=` from Task 3.
- Produces:
  - `headgate.errors.RefusedError`, with the same constructor
  - `headgate.ResponseCheck = Callable[[httpx2.Response], RefusedError | None]`
  - `HttpOptions(response_check=...)`.

- [ ] **Step 1: Write the failing tests:**
  - `test_a_response_check_refuses_a_sign_in_redirect`: rename the existing host-check test in `tests/client/test_client.py` (`_key_page_refusal`) and pass `HttpOptions(response_check=..., follow_redirects=False)`.
  - `test_a_response_check_requires_no_redirects`: `HttpOptions(response_check=check)` with `follow_redirects=True` raises `ValueError` with exactly the new message.
  - `test_the_old_refusal_check_keyword_is_refused` (Review Focus 5): `HttpOptions(refusal_check=check)` raises `TypeError`.
  - Extend `test_the_old_builders_are_gone`: `RefusalCheck` and `RefusedError` are not attributes of `headgate`, and `headgate.errors.RefusedError` is the class readers raise.
- [ ] **Step 2:** Run `uv run python -m pytest tests/client/test_client.py tests/decoding/test_reader.py -v`. Expected: FAIL. `response_check` is an unknown keyword.
- [ ] **Step 3:** Implement the rename and the move as in **Files**. If the error-messages snapshot changes, review the diff and update the snapshot: only `refusal_check` → `response_check` may change.
- [ ] **Step 4:** Update every use found by the grep in **Files**. In `docs/responses.md`, keep the two checks in separate sections, titled by what they see: "A check of the response" (`response_check=`) and "A refusal inside the body" (`refuse=`).
- [ ] **Step 5:** Run every gate, and update the API snapshot. Expected: all pass. In the snapshot diff, `RefusalCheck` becomes `ResponseCheck`, `RefusedError` leaves the top level and appears in `headgate.errors`, and `HttpOptions.refusal_check` becomes `response_check`.
- [ ] **Step 6:** Add the CHANGELOG entries under **Changed**: `HttpOptions(refusal_check=)` → `response_check=`, `RefusalCheck` → `ResponseCheck`, and `from headgate import RefusedError` → `from headgate.errors import RefusedError`. Commit: `Split the refusal checks by what they see, and move RefusedError to headgate.errors`.

### Task 5: Verify the phase and open its PR

- [ ] **Step 1:** Run every gate from a clean tree, and the docs build (`uv run --locked --only-group docs zensical build --strict --clean`). Expected: all pass.
- [ ] **Step 2:** Run `grep -rnE "decoder_for|encoder_for|json_reader|json_writer|refusal_check|RefusalCheck|from headgate import[^\n]*RefusedError|headgate\.(msgspec|pydantic)\.(decoder|encoder)\b" src tests docs examples README.md CHANGELOG.md`. Expected: matches only in the CHANGELOG's own entries.
- [ ] **Step 3:** Dispatch the whole-branch review. Fix what it finds, as separate commits.
- [ ] **Step 4:** Push and open the PR, titled `Phase 2 of the API redesign: readers are a real type`. The body lists the breaking changes, each with its replacement, and links the spec section.
