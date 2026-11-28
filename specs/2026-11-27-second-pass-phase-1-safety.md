# Second pass, phase 1: Safety at the seams — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every credential an auth stamps is declared and cannot be misspelled into a leak, and the secure defaults that
hold for headgate's own pager, URL, body and token code hold for a user's in the same seam.

**Architecture:** Eight small changes at the seams, one task each (two share a task). `auth=` takes a new abstract base
class, `headgate.Auth`, so the credential guard and the cache no longer have an undeclared-auth case. One check refuses
a declaration split into letters wherever credential names are read. GET and HEAD lose `body=`, and the cache passes
any request with a body by. The page walk, not the built-in strategies, resolves and vets a next-page URL, so a call
can refuse a relative `httpx2.URL`. One allowlist encodes every query and form value. Collections of names are typed
`Collection` and refuse a bare `str`. A `CachedTokenAuth` whose token is not text must attach it itself.

**Tech Stack:** Python 3.14, httpx2 2.16, hishel 2, ty, ruff, pytest, syrupy (API snapshot).

**Spec:** `specs/2026-11-27-api-second-pass-design.md`, section 1 (1.1–1.8), approved 2026-11-27.

## Global Constraints

- Python 3.14 or newer; httpx2 2.16 or newer; hishel 2.
- Branch: `claude/api-second-pass-1-safety`, from `main` plus the spec commit. No push and no PR in this phase's run.
- Clean break: no deprecation aliases, no old spelling kept next to the new one; never fix a finding by documenting it.
- Gates for every task:
  - `uv run ruff check`
  - `uv run ruff format --check`
  - `uv run ty check`
  - `uv run typos`
  - `uv run python -m pytest --cov` at 100% line and branch coverage, one process (no xdist)
  - No `noqa`, `ty: ignore` or `pragma: no cover`.
- A change to the public API updates the snapshot with `uv run python -m pytest tests/package/test_api_surface.py --snapshot-update`, with the diff reviewed.
- Every exported name appears in `docs/`, and a test enforces it.
- American English in code, docs and messages.
- Each task adds a `CHANGELOG.md` entry under `## [Unreleased]` (**Added**, **Changed**, **Removed**, **Fixed** or **Security**), saying what to write instead.
- Docstrings describe what the code does now, in the present tense.
- The property tests in `tests/retry/test_properties.py` keep their properties and assertions.
- Each changed behavior has a test that fails before the change; each reviewer repro becomes a real test.

## Decision: `headgate.Auth` is an abstract base class

`headgate.Auth(httpx2.Auth, ABC)` declares `credential_headers` and `credential_params` as abstract read-only
properties and `credential_identity()` as an abstract method. The five auths derive from it. A protocol was the
alternative; the base class is chosen because:

1. **1.2 needs a class-definition hook.** The spec refuses a letter-split declaration "at class definition" for `Auth`
   subclasses. Only a base class runs `__init_subclass__` for a user's class; a protocol is matched structurally and
   never sees the class being defined.
2. **An auth is already a class.** httpx2's `Auth` is a class every auth subclasses, so the base adds no new kind of
   thing to write: `class KeyAuth(headgate.Auth)` instead of `class KeyAuth(httpx2.Auth)`.
3. **A forgotten member fails at construction**, with Python's own message naming it ("Can't instantiate abstract class
   KeyAuth without an implementation for abstract method 'credential_identity'"), and the runtime check in `Client()` is
   one `isinstance`, not a structural match that a typo'd attribute would pass by a `property` returning nothing.

A class attribute (`credential_headers = frozenset({"x-api-key"})`) overrides an abstract property, for ABCs and for
ty alike. `CredentialStamper` and `CredentialCarrier` stay in `headgate.security`: the cache reads a middleware that
is also a stamper, and `CassetteFilter(auths=)` takes carriers (1.7).

## Review Focus

1. **A user's `Auth` that declares through a property** (as `StaticHeaderAuth` does) is accepted and its header is
   withheld cross-origin; a class attribute is checked when the class is defined. Covered in Task 2.
2. **A `stream("GET", body=...)`**, which keeps `body=`, is never served from or stored in the cache. Covered in Task 5.
3. **A custom pager's relative next URL** resolves against the URL that answered (after a redirect), not `base_url`.
   Covered in Task 6.
4. **An `IntEnum` or `StrEnum` value** in `params=` is sent as its value (`3`, `open`), as a plain `Enum`'s is, and a
   `datetime` with a time zone keeps its offset. Covered in Task 7.
5. **A generator passed where a collection of names goes** (`HttpCache(methods=(m for m in ...))`) is refused rather
   than silently consumed. Covered in Task 8.

---

### Task 1: Commit the plan

- [ ] **Step 1:** `uv run typos specs/`. Expected: no output.
- [ ] **Step 2:** `git add specs/2026-11-27-second-pass-phase-1-safety.md && git commit -m "Plan phase 1 of the second API pass: safety at the seams"`.

### Task 2: `auth=` takes a declared `headgate.Auth` (1.1)

**Files:**
- Modify: `src/headgate/_auth.py`: add `Auth`; the five auths derive from it (`StaticHeaderAuth(Auth)`,
  `QueryParamAuth(Auth)`, `SigningAuth(Auth)`, `CachedTokenAuth[TokenT = str](Auth)`); the module docstring.
- Modify: `src/headgate/__init__.py`: export `Auth` (docstring's auth line too).
- Modify: `src/headgate/_client.py`: `auth: Auth | None`, the runtime check, the constructor docstring (drop the
  `ClientConfigurationError` paragraph).
- Modify: `src/headgate/_stack.py`, `src/headgate/security/_credentials.py`, `src/headgate/cache/_credentials.py`:
  type `auth` as `Auth | None`; the guard drops its `isinstance(auth, CredentialStamper)` branch and the module
  docstring's "is not covered" sentence; `credential_scope` drops its `ClientConfigurationError`;
  `mark_caller_credentials` returns `None` for `None` and wraps every `Auth`.
- Modify: `src/headgate/cache/_policy.py`: `Partition` docstrings stop describing an undeclared auth.
- Modify docs: `docs/auth.md` opening line and a "Writing your own auth" example; `docs/security.md` "not covered"
  paragraph; `docs/configuration.md` `auth` bullet; `docs/caching.md` wherever it names an undeclared auth.
- Modify: every test and benchmark auth that subclasses `httpx2.Auth` and is passed to `Client(auth=)` (find with
  `grep -rn "(httpx2.Auth)" tests benchmarks`); ones that exercise a bare `httpx2.AsyncClient` stay as they are.
- Test: `tests/auth/test_auth_base.py` (new), `tests/security/test_credential_origins.py`, `tests/cache/test_credentials.py`.

**Interfaces:**
- Produces: `class Auth(httpx2.Auth, ABC)` in `headgate._auth`, exported as `headgate.Auth`, with abstract
  `credential_headers: frozenset[str]` (property), `credential_params: frozenset[str]` (property) and
  `credential_identity(self) -> bytes`. `Auth.__init_subclass__` is where Task 3 adds the class-attribute check.
- Produces: `Client(..., auth: Auth | None = None, ...)`. A non-`Auth` raises
  `TypeError("auth= takes a headgate.Auth, which declares what it stamps (got KeyAuth): subclass headgate.Auth, or use StaticHeaderAuth, and declare the headers and query parameters it sets in credential_headers and credential_params")`,
  after `reject_upstream_httpx`.

- [ ] **Step 1: Write the failing tests** in `tests/auth/test_auth_base.py`:
  - `test_a_plain_httpx2_auth_is_refused`, parametrized over a 3-line `httpx2.Auth` stamping `X-Api-Key`,
    `httpx2.BasicAuth("u", "p")` and `httpx2.DigestAuth("u", "p")`: `Client(base_url=..., auth=item)` raises
    `TypeError` matching `"auth= takes a headgate.Auth"` and naming the class.
  - `test_a_declared_auth_is_withheld_cross_origin` (port of `ext_plain_auth.py` / T1): a `headgate.Auth` subclass
    with `credential_headers = frozenset({"x-api-key"})`; the API answers `302 Location: https://evil.example/steal`;
    assert `evil.example` receives no `x-api-key` and `api.example` does.
  - `test_a_declaration_through_properties_is_accepted` (Review Focus 1): the same with `@property` members.
  - `test_an_auth_missing_a_member_cannot_be_built`: a subclass without `credential_identity` raises `TypeError` at
    instantiation, naming `credential_identity`.
  - `test_every_headgate_auth_is_an_auth`: the five classes are `issubclass(..., headgate.Auth)`.
  - Delete the cache test of the `ClientConfigurationError` for an undeclared auth, and the guard tests that assert an
    undeclared auth's custom header rides cross-origin; they test the removed carve-out.
- [ ] **Step 2: Run** `uv run python -m pytest tests/auth/test_auth_base.py -q --no-cov`. Expected: FAIL (`Auth` cannot be imported).
- [ ] **Step 3: Implement** `Auth`, the bases, the `Client` check and the typing through the stack; remove the
  carve-out and the cache's error. Update the tests and benchmarks that pass a plain `httpx2.Auth`.
- [ ] **Step 4: Run the gates.** Update the snapshot (new `Auth`, `auth: 'Auth | None'`, the five bases).
- [ ] **Step 5: Docs and changelog.** **Added**: `headgate.Auth`. **Changed**: `auth=` takes a `headgate.Auth`; a
  plain `httpx2.Auth` (`BasicAuth`, `DigestAuth` included) raises `TypeError`: subclass `headgate.Auth` and declare
  what it stamps, or use `StaticHeaderAuth({"Authorization": ...})`. **Removed**: the cache's
  `ClientConfigurationError` for an undeclared auth. **Security**: an auth's custom header no longer follows a
  cross-origin redirect.
- [ ] **Step 6: Commit:** `git commit -am "Take only a declared headgate.Auth as auth=, so no auth's credential follows a request off its origin"`.

### Task 3: A declaration cannot split into letters (1.2)

**Files:**
- Modify: `src/headgate/_core/_kinds.py`: add `check_not_letters`.
- Modify: `src/headgate/security/_credentials.py`: `check_names` calls it for each declaration.
- Modify: `src/headgate/_auth.py`: `Auth.__init_subclass__` runs `check_names` on each declaration that is a class
  attribute (not a descriptor); `SigningAuth`/`CachedTokenAuth` keep theirs.
- Modify: `src/headgate/_options.py`: `check_middleware` calls it.
- Modify: `src/headgate/testing/_recording.py`: `CassetteFilter` calls it on `headers=`, `params=` and each carrier's declarations.
- Test: `tests/core/test_kinds.py`, `tests/auth/test_auth_base.py`, `tests/client/test_middleware.py`, `tests/testing/test_recording.py`.

**Interfaces:**
- Produces: `check_not_letters(where: str, names: Collection[str]) -> None`. For a non-empty *names* all one character
  long it raises `TypeError(f"{where} holds only one-character names ({...sorted, comma-joined}), as frozenset('X-Api-Key') does, which splits a name into its letters: write frozenset({{'X-Api-Key'}})")`.

- [ ] **Step 1: Write the failing tests** (port of T2's repro):
  - `test_a_middleware_declaring_letters_is_refused`: `credential_headers = frozenset("X-Api-Key")` on a middleware →
    `Client(...)` raises `TypeError` matching `"splits a name into its letters"` and `"frozenset\\(\\{'X-Api-Key'\\}\\)"`.
  - `test_an_auth_declaring_letters_is_refused_when_defined`: defining a `headgate.Auth` subclass with that class
    attribute raises the same; a `SigningAuth` subclass with `signed_headers = frozenset("X-Sig")` too.
  - `test_cassette_filter_refuses_letters`: `CassetteFilter(headers=frozenset("x-api-key"))` raises.
  - `test_one_real_one_character_name_beside_others_passes`: `frozenset({"x", "x-api-key"})` is accepted.
- [ ] **Step 2: Run** them with `--no-cov`. Expected: FAIL with "DID NOT RAISE".
- [ ] **Step 3: Implement**, then run the gates. The snapshot does not change.
- [ ] **Step 4: Changelog**, **Security**: a credential declaration whose names are all one character long
  (`frozenset("X-Api-Key")`) is refused with a `TypeError`; write `frozenset({"X-Api-Key"})`.
- [ ] **Step 5: Commit:** `git commit -am "Refuse a credential declaration split into its letters"`.

### Task 4: A token that is not text is attached by its owner (1.8)

**Files:**
- Modify: `src/headgate/_auth.py`: `CachedTokenAuth.__init_subclass__` and `attach`, and the class docstring.
- Test: `tests/auth/test_cached_token_auth.py`.

**Interfaces:**
- Produces: at class definition, a subclass whose own `__orig_bases__` holds `CachedTokenAuth[X]` with `X` (or its
  `get_origin`) a class that is not a `str` subclass, and whose `attach` is still `CachedTokenAuth.attach`, raises
  `TypeError(f"{cls.__name__} holds a {type_text(X)} token, so it must override attach: the default sends a str as Authorization: Bearer")`.
  Otherwise (a type variable, a `Literal`) the default `attach` raises `TypeError` for a non-`str` token, naming the
  class and the token's type, before anything is set on the request.

- [ ] **Step 1: Write the failing tests** (port of `ext_mw_misuse.py` / E8):
  - `test_a_struct_token_without_attach_is_refused_when_defined`: `class T(CachedTokenAuth[dict[str, str]])` with a
    mint and no `attach` raises `TypeError` matching `"must override attach"`.
  - `test_a_generic_subclass_is_refused_at_the_first_attach`: `class Mid[T](CachedTokenAuth[T])`, then
    `class Leaf(Mid[dict[str, str]])` is defined; a call raises `TypeError` and the fake transport received nothing.
  - `test_a_struct_token_with_attach_is_accepted`: with an `attach` override that sends `token["access"]`, the header is
    `Bearer abc`.
- [ ] **Step 2: Run** them with `--no-cov`. Expected: FAIL.
- [ ] **Step 3: Implement**, run the gates.
- [ ] **Step 4: Changelog**, **Fixed**: a `CachedTokenAuth` whose token type is not `str` must override `attach`;
  the default no longer sends the token's `repr` as the bearer.
- [ ] **Step 5: Commit:** `git commit -am "Require CachedTokenAuth to attach a token that is not text itself"`.

### Task 5: A GET or HEAD has no body (1.3)

**Files:**
- Modify: `src/headgate/_client.py`: `get` and `head` (and `head`'s overloads) take `**options: Unpack[CallOptions]`;
  `_request` checks GET and HEAD against `CALL_KEYS` with a hint for every body keyword.
- Modify: `src/headgate/cache/_transport.py`: `ResilientCacheTransport.handle_async_request` sends a request with a
  body straight to the live transport (never looked up, stored or shared); class docstring.
- Modify docs: `docs/caching.md`, wherever a body on a read is described; `docs/responses.md` or `docs/configuration.md` if they show `get(..., body=)`.
- Test: `tests/client/test_verbs.py`, `tests/cache/test_rules.py`, `tests/client/test_call_options_types.py`.

**Interfaces:**
- Produces: `get(...)`/`head(...)` with `body=` (or `json=`, `data=`, `content=`, `files=`) raise
  `TypeError("get() got an unexpected keyword argument 'body': a GET has no body, and the cache does not key one: use query(), the safe read with a body")`
  (`head()`/"a HEAD" for HEAD).
- Produces: a private `_has_body(request: httpx2.Request) -> bool` in `cache/_transport.py`: a `Content-Length` other
  than `0`, or a `Transfer-Encoding`.

- [ ] **Step 1: Write the failing tests** (port of `r04_getbody_cache.py` / T3):
  - `test_get_and_head_refuse_a_body`: parametrized over `get`/`head` and the five keywords; `TypeError` matching `"use query\\(\\)"`, nothing sent.
  - `test_a_body_on_get_does_not_type_check`: ty probe (the house style of `test_call_options_types.py`) reports `body` on `get`.
  - `test_the_cache_passes_a_bodied_request_by` (Review Focus 2): a cached client opens `stream("GET", ..., body=Json({"q": "alice"}))` then the same with `"bob"`; the server is reached twice and answers each with its own body; the second is no `HIT`.
- [ ] **Step 2: Run** them with `--no-cov`. Expected: FAIL.
- [ ] **Step 3: Implement**, run the gates, update the snapshot (`get`/`head` signatures).
- [ ] **Step 4: Changelog**, **Changed**: `get()` and `head()` take no `body=`; use `query()`. **Security**: the cache
  never looks up, stores or shares a request with a body.
- [ ] **Step 5: Commit:** `git commit -am "Refuse a body on GET and HEAD, and keep bodied requests out of the cache"`.

### Task 6: The walk owns its URL rules (1.4)

**Files:**
- Modify: `src/headgate/pagination/_paginated.py`: `PageWalk.pages` resolves a next request whose URL is an
  `httpx2.URL` against `response.url`, and refuses `https` to `http`, inside the `try` that maps `InvalidURL`.
- Modify: `src/headgate/pagination/_pager.py`: `_absolute(target: str) -> PageRequest` becomes
  `PageRequest(httpx2.URL(target))`; module docstring.
- Modify: `docs/pagination.md`: the custom-pager section stops telling the author to resolve URLs.
- Test: `tests/pagination/test_paginate.py`.

**Interfaces:**
- Produces: a private `_next_url(request: PageRequest[B], response: httpx2.Response) -> PageRequest[B]` in
  `_paginated.py`, raising `ForbiddenDestinationError("refused to follow a next page from https to <loggable url>")`
  for a downgrade. Task 7 relies on every `httpx2.URL` a walk sends being absolute.

- [ ] **Step 1: Write the failing tests** (port of `ext_pager.py` / E2):
  - `test_a_custom_pager_cannot_downgrade_the_walk`: a pager returning `PageRequest(httpx2.URL("http://api.example/things2?cursor=secret"))` from an `X-Next-Url` header; iterating raises `ForbiddenDestinationError`, and only one request was sent.
  - `test_a_custom_pager_relative_url_resolves_against_the_answering_url` (Review Focus 3): the first page redirects to `https://api.example/v2/things`, the pager returns `httpx2.URL("page2")`; the second request is `https://api.example/v2/page2`.
- [ ] **Step 2: Run** them with `--no-cov`. Expected: FAIL (the http request is sent; the URL resolves against `base_url`).
- [ ] **Step 3: Implement**, run the gates. The snapshot does not change.
- [ ] **Step 4: Changelog**, **Security**: every pager's next URL is resolved against the URL that answered, and a
  next page from `https` to `http` is refused, as the built-in pagers already did.
- [ ] **Step 5: Commit:** `git commit -am "Resolve and vet every pager's next URL in the walk, not in the built-in pagers"`.

### Task 7: An `httpx2.URL` is absolute, and a URL is of its kind (1.5)

**Files:**
- Modify: `src/headgate/_client.py`: `_prepare` checks the URL's kind and refuses a relative `httpx2.URL`; the
  **URLs** paragraph of the `Client` docstring.
- Modify: `src/headgate/_url.py`: `CallUrl`'s docstring says "absolute `httpx2.URL`".
- Modify docs: wherever `httpx2.URL(...)` is shown relative (`grep -rn "httpx2.URL(" docs README.md examples`).
- Test: `tests/urls/test_call_url.py` (its relative `httpx2.URL` test becomes a refusal test).

**Interfaces:**
- Produces: `TypeError("a relative httpx2.URL skips escaping: write a t-string, t'items/{uid}', or {fragment:raw} for a pre-built fragment (got 'items/../admin')")`
  for a relative URL, and `check_kind("url", url, (str, Template, httpx2.URL), "a literal str, a t-string or an absolute httpx2.URL")` for any other kind.

- [ ] **Step 1: Write the failing tests** (port of `r01_urls.py` / T4, T11):
  - `test_a_relative_url_object_is_refused`: `get(httpx2.URL("items/../admin"))` raises `TypeError` matching `"t-string"`; nothing sent.
  - `test_a_url_of_another_kind_is_refused`: parametrized over `None`, `b"items"`, `["items"]`, `3`; `TypeError` matching `"url= takes"`.
  - Keep `test_an_absolute_url_object_is_sent_as_given`.
- [ ] **Step 2: Run** them with `--no-cov`. Expected: FAIL.
- [ ] **Step 3: Implement**, run the gates.
- [ ] **Step 4: Changelog**, **Changed**: a relative `httpx2.URL` is refused; write a t-string. A URL of another kind
  (`None`, `bytes`) raises `TypeError` instead of being sent as its text.
- [ ] **Step 5: Commit:** `git commit -am "Refuse a relative httpx2.URL and a URL of another kind"`.

### Task 8: Only values with a wire meaning are sent (1.6)

**Files:**
- Modify: `src/headgate/_request_body.py`: add `wire_value` and `wire_fields`; `Form.__post_init__` and
  `Multipart.__post_init__` store `wire_fields(...)` of their fields.
- Modify: `src/headgate/_client.py`: `_query_pairs` uses `wire_value`; `_query_value` and `_NESTED` go.
- Modify: `src/headgate/_call_options.py`: the `params` bullet of `CallOptions`.
- Modify docs: `docs/configuration.md` (the scalar list that names "an `Enum` member"), and `docs/responses.md` / body docs for `Form`.
- Test: `tests/client/test_request_body.py`, `tests/client/test_call_options.py`.

**Interfaces:**
- Produces: `wire_value(option: str, key: str, value: object) -> str | int | float | bool | None`. `str`, `int`,
  `float`, `bool`, `None` as they are; an `Enum` (checked first) as its `.value`, which must be a `str` or `int`;
  `UUID` and `Decimal` as `str()`; `date`, `datetime`, `time` as `isoformat()`; anything else raises
  `TypeError(f"{option} takes a str, int, float, bool, None, an Enum of str or int, a UUID, Decimal, date, datetime or time for each value, or a list or tuple of them (got {type} under {key!r}): encode any other value into text first, such as with json.dumps")`.
- Produces: `wire_fields(option: str, fields: Mapping[str, object]) -> dict[str, object]`: each value through
  `wire_value`, a list or tuple item by item.

- [ ] **Step 1: Write the failing tests** (port of F1 / `r15_params.py` / T8):
  - `test_params_send_only_wire_values`: `Status.OPEN` (plain `Enum`) → `status=open`, `IntEnum` → `3`, `StrEnum` → its value, `UUID` → its text, `Decimal("1.50")` → `1.50`, `datetime(2026, 1, 2, 3, 4, tzinfo=UTC)` → `2026-01-02T03:04:00+00:00` (Review Focus 4).
  - `test_params_refuse_a_value_with_no_wire_meaning`: a dataclass, a msgspec `Struct`, `object()`, a dict, a nested list, an `Enum` with a `float` value; `TypeError` naming the key.
  - `test_form_and_multipart_fields_use_the_same_allowlist`: `Form({"a": Status.OPEN})` sends `a=open`; `Form({"a": {"k": 1}})` and `Multipart(files, fields={"a": D(1)})` raise at construction.
- [ ] **Step 2: Run** them with `--no-cov`. Expected: FAIL (`Status.OPEN` sent; no `TypeError`).
- [ ] **Step 3: Implement**, run the gates.
- [ ] **Step 4: Changelog**, **Changed**: `params=`, `Form` and `Multipart` fields send an `Enum` as its value, `date`
  and `datetime` as ISO 8601, and refuse any other value that is not text, a number, a bool or `None` with a
  `TypeError`, instead of sending its `repr`: encode it first.
- [ ] **Step 5: Commit:** `git commit -am "Send only values with a wire meaning in queries and forms"`.

### Task 9: Collections of names are collections (1.7)

**Files:**
- Modify: `src/headgate/testing/_recording.py`: `CassetteFilter(auths: Iterable[CredentialCarrier], headers: Collection[str], params: Collection[str])`; refuse a non-carrier entry.
- Modify: `src/headgate/cache/_policy.py`: `methods: Collection[str] | None`, `paths: Collection[str | re.Pattern[str]] | None`, `statuses: Collection[int] | None`.
- Modify: `src/headgate/retry/_policy.py`: `methods: Collection[str]`, `statuses: Collection[int]`.
- Test: `tests/testing/test_recording.py`, `tests/cache/test_rules.py`, `tests/retry/test_policy.py`.

**Interfaces:**
- Consumes: `headgate._core.check_names(option, value, wanted)` for names, `check_collection` for `paths` and
  `statuses` (their items are not all `str`), and `check_kind` for each `auths` entry against `CredentialCarrier`.
- Produces: e.g. `headers= takes a collection of header names, such as ('x-api-key',) (got str)`.

- [ ] **Step 1: Write the failing tests** (port of T9):
  - `test_cassette_filter_refuses_a_bare_string`: `headers="x-api-key"` and `params="api_key"` raise `TypeError`.
  - `test_cassette_filter_refuses_an_entry_that_declares_nothing`: `auths=["token"]` raises `TypeError`.
  - `test_http_cache_refuses_a_bare_string`: `methods="GET"`, `paths="/items"` raise `TypeError`; `statuses=200` too.
  - `test_http_cache_refuses_a_generator` (Review Focus 5).
  - `test_retry_policy_refuses_a_bare_string`: `RetryPolicy(methods="GET")` and `statuses=503` raise `TypeError`.
- [ ] **Step 2: Run** them with `--no-cov`. Expected: FAIL.
- [ ] **Step 3: Implement**, run the gates, update the snapshot (the parameter types).
- [ ] **Step 4: Changelog**, **Changed**: these parameters take collections, and a bare `str`, a number or a one-shot
  iterator raises `TypeError`: write `("GET",)`. **Security**: `CassetteFilter(headers="x-api-key")` no longer records
  the header it was asked to scrub.
- [ ] **Step 5: Commit:** `git commit -am "Refuse a bare string where a collection of names goes"`.

### Task 10: Verify the phase

- [ ] **Step 1:** Run every gate from scratch: ruff, the format check, ty, typos, `uv run python -m pytest --cov` (100%).
- [ ] **Step 2:** `uv run --group docs zensical build --strict --clean` if the docs group is installed; else note it.
- [ ] **Step 3:** `grep -rn "is not covered\|httpx2.Auth\b" docs README.md src` shows no carve-out left.
