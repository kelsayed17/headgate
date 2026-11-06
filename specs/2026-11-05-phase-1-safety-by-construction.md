# Phase 1: Safety by construction — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the safe spelling the only one that type-checks for URLs, and the only one that runs for request signing and middleware credentials.

**Architecture:** Three independent changes to the public surface. URLs gain a type, `LiteralString | Template | httpx2.URL`, used by every call site. `Middleware` must declare its credentials, checked when `HttpOptions` is built. `SigningAuth` stops signing in its auth flow: the flow marks the request, and `MiddlewareTransport`, which sits inside the retry layer, signs each attempt just before the credential guard.

**Tech Stack:** Python 3.14, httpx2 2.16, ty, ruff, pytest, syrupy (API snapshot).

**Spec:** `2026-11-05-public-api-redesign-design.md`, section 1 (Safety by construction), approved 2026-11-05. It sits beside this plan, and Task 1 commits both into the repo under `specs/`.

## Global Constraints

- Python 3.14 or newer; httpx2 2.16 or newer; hishel 2.
- Base branch: `main` after PR #54 (bug fixes, `retry: bool | RetryPolicy | None = None`) and PR #55 (hishel 2) have merged. If either is still open, stop and ask; do not branch from them.
- Clean break: no deprecation aliases, and no old spelling kept next to the new one.
- Gates for every task:
  - `uv run ruff check`
  - `uv run ruff format --check`
  - `uv run ty check`
  - `uv run typos`
  - `uv run python -m pytest --cov` at 100% line and branch coverage
  - No `noqa`, `ty: ignore` or `pragma: no cover`.
- A change to the public API updates the snapshot with `uv run python -m pytest tests/package/test_api_surface.py --snapshot-update`, with the diff reviewed.
- Every exported name appears in `docs/`, and a test enforces it.
- American English in code, docs and messages.
- Each task adds a `CHANGELOG.md` entry under `## [Unreleased]`, in **Changed** for a break and **Fixed** for a fix, saying what to write instead.
- Docstrings describe what the code does now, in the present tense.
- The property tests in `tests/retry/test_properties.py` keep their properties and assertions.

## Review Focus

These five things are not covered by the spec's own examples and are most likely to bite users. Each line gets a test in the task named.

1. **A next-page URL forwarded from the wire** (`LinkHeader`, `NextUrl`, a body cursor URL) still goes out verbatim, percent-escapes included, and keeps its query. Covered in Task 2.
2. **A hedged second attempt is signed afresh**, and a retry is too: no two attempts carry the same signature. Covered in Task 4.
3. **A cross-origin redirect hop does not carry the signature**, and a 401 from that hop is not re-sent. The signature is applied before the credential guard, so the guard can withhold it. Covered in Task 4.
4. **A request the caller signed is never signed** by headgate on any attempt. `skips()` is decided once, in the flow. Covered in Task 4.
5. **A middleware that declares its credentials through properties** (as headgate's auths do) is accepted. A bare `async def` function, or a declaration given as a `set`, `list` or `str`, is refused with a message naming the class or function. Covered in Task 3.

---

### Task 1: Branch, and commit the spec and plan

**Files:**
- Create: `specs/2026-11-05-public-api-redesign-design.md` (copy of the approved spec)
- Create: `specs/2026-11-05-phase-1-safety-by-construction.md` (this plan)

**Interfaces:**
- Produces: the branch `claude/api-redesign-phase-1`, cut from `origin/main`, in its own worktree.

- [ ] **Step 1:** Create the worktree and branch from `origin/main`. Check that `git log origin/main` contains PRs #54 and #55.
- [ ] **Step 2:** Copy both documents into `specs/`. `specs/` sits outside `docs/`, so the site build does not publish them.
- [ ] **Step 3:** Run `uv run typos specs/`. Expected: no output.
- [ ] **Step 4:** Commit: `git add specs && git commit -m "Add the public API redesign spec and its phase 1 plan"`.

### Task 2: URLs are `LiteralString | Template | httpx2.URL`

**Files:**
- Modify: `src/headgate/_url.py`, adding the alias `CallUrl`.
- Modify: `src/headgate/_client.py`:
  - every `url: str | Template` parameter (14 of them: the verbs and their overloads, `stream`, `paginate` and its overloads)
  - `_Call.url`
  - `_prepare`, around the `isinstance(call.url, Template)` branch.
- Modify: `src/headgate/pagination/_pager.py`: `PageRequest.url`, and `_absolute`, which returns `PageRequest(response.url.join(target))` with no `str(...)`.
- Modify: `src/headgate/pagination/_paginated.py`, lines 160 and 170: `isinstance(request.url, str)` becomes `isinstance(request.url, str | httpx2.URL)`, and the URL is turned into text with `str(...)` before `loggable_url`.
- Modify docs:
  - the `AsyncBaseClient` docstring section **URLs**
  - `docs/responses.md` or `docs/configuration.md`, wherever "a `str` URL is passed through verbatim" is said (find it with `grep -rn "verbatim" docs src`)
  - `docs/pagination.md`, where a custom pager returns a next URL.
- Modify `examples/`: any `get(f"...")` with an interpolated value becomes a t-string.
- Test:
  - `tests/urls/test_call_url.py` (new)
  - `tests/urls/test_call_url_types.py` (new: the type-checker test)
  - `tests/pagination/test_paginate.py`, for Review Focus 1.

**Interfaces:**
- Produces: `type CallUrl = LiteralString | Template | httpx2.URL` in `headgate._url`. It is exported nowhere public, because the signatures spell it out through the alias. Every verb, `stream`, `paginate` and `PageRequest.url` use it.
- Produces: a `Pager` that returns `PageRequest(httpx2.URL(...))` for a wire URL. `PageRequest(str)` remains valid for a literal.

- [ ] **Step 1: Write the failing runtime tests** in `tests/urls/test_call_url.py`:
  - `test_a_url_object_is_sent_verbatim`: `get(httpx2.URL("items/a%2Fb?x=1"), expect=nothing)` sends `raw_path == b"/items/a%2Fb?x=1"` (base `https://api.test/`). Its route in the `CallFinished` event is `None`.
  - `test_an_absolute_url_object_is_sent_as_given`: `get(httpx2.URL("https://other.test/v2/x"))` goes to `https://other.test/v2/x`, under the same credential rules as a `str` today. Assert the `Authorization` from a `BearerAuth` is withheld, as `tests/security` does for a cross-origin `str`.
  - `test_a_t_string_is_still_escaped`: `t"items/{'a/b'}"` sends `raw_path == b"/items/a%2Fb"`.
- [ ] **Step 2: Write the failing type test** in `tests/urls/test_call_url_types.py`, `test_a_computed_string_url_does_not_type_check`:
  - Write a module to `tmp_path / "probe.py"` that builds a `StubClient` and calls `await c.get(f"items/{uid}", expect=nothing)` with `uid: str`.
  - Run `[sys.executable, "-m", "ty", "check", str(path)]` with `subprocess.run(..., capture_output=True, text=True, check=False)`.
  - Assert `returncode != 0` and that `"LiteralString"` appears in `stdout`.
  - A second probe calls `get("items/1")`, `get(t"items/{uid}")` and `get(httpx2.URL(next_url))`; assert it exits `0`.
  - Add no marker; `--strict-markers` is on and no `slow` marker exists. ty reports `Expected `LiteralString | Template | URL`, found `str``, so assert that substring.
- [ ] **Step 3: Run the tests to verify they fail.** Run `uv run python -m pytest tests/urls -q --no-cov`. Expected: the type test fails (exit `0` on the f-string probe), and the `httpx2.URL` tests fail in `_prepare` or in `loggable_url`.
- [ ] **Step 4: Implement.**
  - Add `CallUrl` and use it in every signature listed above.
  - In `_prepare`: a `Template` renders as today; an `httpx2.URL` or `str` becomes `str(call.url)`, with route `None`.
  - Pagers forward `httpx2.URL`.
- [ ] **Step 5: Add the Review Focus 1 test** to `tests/pagination/test_paginate.py`, named `test_a_next_url_from_the_wire_is_sent_verbatim`. A `NextUrl` pager reads `"https://api.test/items?cursor=a%2Fb&x=1"` from a body, and the second request's `raw_path` is exactly `b"/items?cursor=a%2Fb&x=1"`.
- [ ] **Step 6: Run the gates.** All pass, except the API snapshot, which shows only the `url:` annotations changing. Update it.
- [ ] **Step 7: Add the changelog entry**, under **Changed**: "A call's URL is typed `LiteralString | Template | httpx2.URL`. A computed `str`, such as an f-string, no longer type-checks, because it skipped escaping: write a t-string for a path built from values, or `httpx2.URL(...)` for a URL taken from a response. A `str` is still accepted at runtime."
- [ ] **Step 8: Commit:** `git commit -am "Type a call's URL so a computed string no longer type-checks"`.

### Task 3: A middleware must declare its credentials

**Files:**
- Modify: `src/headgate/_middleware.py`: the `Middleware` protocol, the module docstring and the protocol docstring.
- Modify: `src/headgate/_options.py`: `HttpOptions.__post_init__` gains `_check_middleware(self.middleware)` next to `_check_hooks("middleware", ...)`.
- Modify: `src/headgate/security/_credentials.py:106` and `src/headgate/cache/_credentials.py:169`. The `isinstance(..., CredentialCarrier)` filters over `http.middleware` go, because every middleware is now a carrier.
- Modify: `docs/configuration.md` § Middleware, so every example declares both names.
- Modify: every test middleware under `tests/` and `benchmarks/` (find them with `grep -rn "middleware=" tests benchmarks`). Each one gets the two class attributes, set to the empty `frozenset[str]()` where it stamps nothing.
- Test: `tests/client/test_middleware.py`.

**Interfaces:**
- Produces: `Middleware` protocol members `credential_headers: frozenset[str]` and `credential_params: frozenset[str]`, declared as read-only properties so both a class attribute and a property satisfy them.
- Produces: `_check_middleware(middleware: Iterable[object]) -> None` in `_options.py`. It raises `TypeError` with the message `f"middleware {name} must declare credential_headers and credential_params as frozensets of names, empty when it stamps none"`, where `name` is `type(item).__qualname__`, or the function's `__qualname__` for a function.

- [ ] **Step 1: Write the failing tests:**
  - `test_a_middleware_that_declares_nothing_is_refused`, parametrized over:
    - no attributes;
    - the typo `credential_header`;
    - only `credential_headers`;
    - `credential_headers = {"x-sig"}` (a `set`);
    - a bare `async def` function.

    Each is passed to `HttpOptions(middleware=(item,))`. Assert `TypeError` with `match="must declare credential_headers and credential_params"`, and that the message names the class or the function.
  - `test_a_middleware_may_declare_through_properties`: a class whose two members are `@property` returning frozensets builds, and its header is withheld on a cross-origin hop. Reuse the existing cross-origin test's transport in `tests/client/test_middleware.py`.
  - Change the existing test of an undeclared middleware riding every hop. It now expects construction to refuse that middleware.
- [ ] **Step 2: Run the tests to verify they fail.** Run `uv run python -m pytest tests/client/test_middleware.py -q --no-cov`. Expected: the refusal tests fail with "DID NOT RAISE".
- [ ] **Step 3: Implement** the protocol members, `_check_middleware`, and the removed `isinstance` filters.
- [ ] **Step 4: Update** every test and benchmark middleware, then run the gates. The snapshot shows the two new `Middleware` members; update it.
- [ ] **Step 5: Add the changelog entry**, under **Changed**: "A middleware must declare `credential_headers` and `credential_params` (each a `frozenset` of names, empty when it stamps none). `HttpOptions` refuses one that does not, with a `TypeError`, since an undeclared or misspelled declaration sent the credential to every origin. A plain function is no longer a middleware; wrap it in a class with the two declarations."
- [ ] **Step 6: Commit:** `git commit -am "Require a middleware to declare the credentials it stamps"`.

### Task 4: `SigningAuth` signs every attempt

**Files:**
- Modify: `src/headgate/_auth.py`: `SigningAuth.async_auth_flow`, the class docstring, and a new constant `SIGNER_EXT`.
- Modify: `src/headgate/_middleware.py`: in `MiddlewareTransport`, the `guarded` step signs before `guard(request)`.
- Modify: `src/headgate/_stack.py:141-142`. The `MiddlewareTransport` is built when `http.middleware` is set **or** `isinstance(credentials.auth, SigningAuth)`.
- Modify: `docs/auth.md` § SigningAuth (it now signs each attempt, so the paragraph about a retry repeating a stale signature goes), and `docs/configuration.md` § Middleware (its HMAC example becomes a `SigningAuth` subclass, and the text stops recommending middleware for signatures). Also update the `_middleware.py` module docstring sentence that recommends a middleware signer, and `docs/design.md`'s stack diagram line for the auth flow.
- Test: `tests/auth/test_signing_auth.py`.

**Interfaces:**
- Consumes: Task 3's `MiddlewareTransport`, unchanged apart from the signing step.
- Produces: `SIGNER_EXT: Final = "headgate.signer"`, the request extension that holds the `SigningAuth` that will sign the request.
- Produces this flow contract:
  - `skips(request)` is decided once. When it returns true, the extension is not set and the request goes out as the caller built it.
  - Otherwise the flow runs `await request.aread()`, sets `request.extensions[SIGNER_EXT] = self` and yields.
  - On a 401 whose `response.request` carries a signed header or parameter (`self._carries(response.request)`), it yields the same request once more.
  - `MiddlewareTransport` calls `signer.sign(request)` on every attempt, after the last middleware and before the credential guard.

- [ ] **Step 1: Write the failing tests:**
  - `test_each_attempt_is_signed_afresh`. Answers are 503, then 200, under `RetryPolicy(attempts=2)` and `instant_retries()`. Assert `X-Nonce` values `["1", "2"]` and the first `X-Signature` differing from the second. This replaces `test_a_retry_that_went_stale_is_accepted_once_signed_again`: delete that test, whose premise (a stale re-send) no longer holds.
  - `test_a_hedged_attempt_is_signed_afresh` (Review Focus 2). Use the hedge setup from `tests/retry/test_parking_and_hedging.py` with a `Hedge` policy, and assert that the two sent requests carry different `X-Nonce` values.
  - `test_a_cross_origin_hop_carries_no_signature` (Review Focus 3). The origin answers 302 to `https://other.test/x`, which answers 401. Assert that the hop to `other.test` has no `X-Signature`, and that exactly one request reached `other.test` (no re-send).
  - `test_a_request_the_caller_signed_is_never_signed` (Review Focus 4). The caller sets `X-Signature: mine`, the server answers 503 then 200, and retries are on. Assert every attempt carries `mine` and `sign()` was never called (the `_Signer`'s counter stays `0`).
  - Keep `test_a_401_is_signed_again_once` with its expected `["1", "2"]` nonces.
- [ ] **Step 2: Run the tests to verify they fail.** Run `uv run python -m pytest tests/auth/test_signing_auth.py -q --no-cov`. Expected: `test_each_attempt_is_signed_afresh` and the hedge test fail with equal nonces.
- [ ] **Step 3: Implement** `SIGNER_EXT`, the flow contract, the signing step in `MiddlewareTransport`, and the condition in `_stack.py`.
- [ ] **Step 4: Run the gates.** Run `tests/retry/test_properties.py` explicitly; it must pass without changes. Check the snapshot: `SIGNER_EXT` is internal, so the snapshot does not change unless a public signature did.
- [ ] **Step 5: Add the changelog entry**, under **Changed**: "`SigningAuth` signs every attempt, inside the retry layer, so a retry or a hedge carries a fresh signature instead of repeating the first. It still re-sends once on a 401, now signed afresh. A signature that must change per attempt belongs in `SigningAuth`, not in a middleware."
- [ ] **Step 6: Commit:** `git commit -am "Sign every attempt in SigningAuth, inside the retry layer"`.

### Task 5: Verify the phase and open its PR

**Files:**
- Modify: `README.md` only if it shows an f-string URL or a middleware signer (check with `grep -n 'get(f"\|middleware' README.md`).

- [ ] **Step 1: Run every gate from scratch:**
  - `uv sync --locked`
  - ruff, the format check, ty and typos
  - `uv run python -m pytest --cov`, expecting 100% and every test passing
  - `uv run --group docs zensical build --strict --clean`, expecting "No issues found"
  - `uv run python -m pytest benchmarks -q --no-cov`.
- [ ] **Step 2: Reproduce the two dependency CI jobs:**
  - `uv sync --resolution lowest-direct && uv run python -m pytest -q`, then restore the lock with `git checkout uv.lock && uv sync --locked`;
  - a fresh venv with `uv pip install .`, then `python -I -c "import headgate"`.
- [ ] **Step 3: Push** `claude/api-redesign-phase-1` and open a PR against `main`. Fill in the repo's template, list the three breaking changes, and link the spec.
