# Second pass, phase 2: Auth, one way — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An auth seam is written one way: one declaration, one mint, no base constructor, a flow that is not a hook,
a signer on the client's clock, and hooks refused when they have the wrong color.

**Architecture:** Six changes to `src/headgate/_auth.py` and its two callers, one task each, then one for the
constructor signatures. `SigningAuth` and `CachedTokenAuth` declare with the `credential_headers` and
`credential_params` class attributes every other seam uses. `CachedTokenAuth.mint(request)` is its one abstract hook;
its state is made on first use, so it has no `__init__`. `async_auth_flow` is `@final` on both. The signing layer
passes `sign` the client's wall-clock time. `Client()` refuses an async observer and a sync middleware.

**Tech Stack:** Python 3.14, httpx2 2.16, ty, ruff, pytest, syrupy (API snapshot).

**Spec:** `specs/2026-11-27-api-second-pass-design.md`, section 2 (2.1–2.6), approved 2026-11-27.

## Global Constraints

- Python 3.14 or newer; httpx2 2.16 or newer; hishel 2.
- Branch: `claude/api-second-pass-2-auth`, stacked on `claude/api-second-pass-1-safety`. No push and no PR in this run.
- Clean break: no deprecation aliases, no old spelling kept next to the new one; never fix a finding by documenting it.
- Gates for every task: `uv run ruff check`, `uv run ruff format --check`, `uv run ty check`, `uv run typos`,
  `uv run python -m pytest --cov` at 100% line and branch coverage in one process, and
  `uv run zensical build --strict` once docs change. No `noqa`, `ty: ignore` or `pragma: no cover`.
- A public API change updates the snapshot (`uv run python -m pytest tests/package/test_api_surface.py --snapshot-update`), diff reviewed.
- Every exported name appears in `docs/`. American English. Docstrings in the present tense.
- Each task adds or extends a `CHANGELOG.md` entry under `## [Unreleased]`, saying what to write instead, beside
  phase 1's; grep afterwards that the text landed.
- `tests/retry/test_properties.py` keeps its properties and assertions.
- Every `SigningAuth` and `CachedTokenAuth` in `docs/`, `README.md`, `examples/`, `benchmarks/` and `tests/` follows each rename.

## Decisions

1. **`timing=` leaves `CachedTokenAuth` now.** 2.3 removes its `__init__`, and the keyword lives there; 3.5 removes it
   from the other four constructors in phase 3. The token's clock is `current_timing().clock`, read when its state is
   made: when a `Client` is built with it (the client touches the state, so the auth runs on the timing the client
   is built under, as the docs already promise), or at its first `seed()` or call if that comes first.
2. **`mint` is declared as a plain `def` returning `AsyncGenerator[...]`**, abstract, so a subclass's `async def mint`
   with `yield` is a valid override for ty.
3. **`@final` is enforced at runtime too.** A subclass that defines `async_auth_flow` or `reject_credential_rotation`
   raises `TypeError` when it is defined, since the override fails deep inside the call (E13's `AttributeError`).
4. **`Auth` gets an explicit `__init__(self) -> None`**, which `SigningAuth` and `CachedTokenAuth` inherit, so all
   three read `()`. It shadows a sibling base's constructor, so the digest recipe becomes
   `class Digest(httpx2.DigestAuth, Auth)`: the wrapped auth first.
5. **A declaration through a property still counts** for the "declares at least one name" checks, as it did for
   `CachedTokenAuth`; it is checked for its kind when the client is built, and its parameters are not registered as
   secret at class definition.

## Review Focus

1. **A `CachedTokenAuth` built and handed to a `Client` inside `TestClock.install()`, then called after the block**,
   still runs its token lifetime on the test clock. Covered in Task 4.
2. **A `SigningAuth` declaring mixed-case names** (`frozenset({"X-Sig"})`) has them withheld cross-origin and its
   `skips` default matches them case-insensitively. Covered in Task 2.
3. **A `CachedTokenAuth` subclass that overrides `attach` but leaves the inherited `{"authorization"}` declaration** is
   refused at definition, as with `token_headers` today. Covered in Task 2.
4. **A middleware that is a plain `async def` function**, and one that is a `functools.partial` of one, are accepted;
   a class with a sync `__call__` is refused. Covered in Task 7.
5. **A retry and a hedge each get their own `now`** from the client's wall clock. Covered in Task 6.

---

### Task 1: Commit the plan

- [ ] `uv run typos specs/`, then `git add specs/2026-11-27-second-pass-phase-2-auth.md && git commit`.

### Task 2: One declaration (2.1)

**Files:** `src/headgate/_auth.py`; docs `auth.md`, `configuration.md`, `security.md` if it names them; `examples/orders_client.py`; every test using `signed_*`/`token_*`.

**Interfaces:**
- `SigningAuth.credential_headers: ClassVar[frozenset[str]] = frozenset()` and `credential_params` likewise;
  `CachedTokenAuth.credential_headers: ClassVar[frozenset[str]] = frozenset({"authorization"})`,
  `credential_params = frozenset()`. The mapping properties, `signed_*` and `token_*` go.
- Refusal messages: `f"{cls.__name__} must declare the headers or parameters sign() sets, in credential_headers or credential_params"`
  and `f"{cls.__name__} overrides attach, so it must declare what attach sets in credential_headers or credential_params"`.

- [ ] **Tests (fail first):** in `tests/auth/test_credential_stampers.py`:
  `test_a_signer_declares_with_credential_headers` (port of E6: a `SigningAuth` with `credential_headers = frozenset({"X-Sig"})`
  signs and the header is withheld on a cross-origin redirect), `test_a_signer_declaring_nothing_is_refused`,
  `test_an_attach_override_must_redeclare` (Review Focus 3), `test_a_token_auth_declares_its_query_parameter`
  (`credential_params = frozenset({"access_token"})` is withheld and redacted).
- [ ] Implement; migrate every `signed_*`/`token_*` spelling. Snapshot: the two classes lose their `credential_*` properties.
- [ ] Changelog **Changed**: `signed_headers`/`signed_params`/`token_headers`/`token_params` become
  `credential_headers`/`credential_params`; extend phase 1's Security entry, which names them.
- [ ] Commit.

### Task 3: One mint (2.2)

**Files:** `_auth.py`; `testing/_transport.py` docstring; docs `auth.md`, `testing.md`, `rate-budgets.md`, `retries.md`; `examples/orders_client.py`; tests.

**Interfaces:**
- `@abstractmethod def mint(self, request: httpx2.Request) -> AsyncGenerator[httpx2.Request | Minted[TokenT], httpx2.Response]`.
- `seed(self, minted: Minted[TokenT]) -> None`. Removed: `mint_token`, `mint_flow`, the "override one of two" check.
- The flow's end-without-`Minted` message names `mint`.

- [ ] **Tests:** `test_a_mint_with_no_hop_yields_only_minted`; `test_an_auth_without_mint_cannot_be_built`
  (`TypeError` naming `mint` at instantiation); `test_seed_takes_a_minted` (port of P15); a mint under
  `fake_network` reaches the fake (port of F2's `probe_mint.py`).
- [ ] Implement; convert every `mint_token`/`mint_flow`/`seed(token, lifetime)`. Remove the docs' `mint_token`
  recipe and the "does not reach the fake" clauses. Snapshot.
- [ ] Changelog **Changed** (`mint`, `seed(Minted(...))`) and **Removed** (`mint_token`, with what to write).
- [ ] Commit.

### Task 4: No base constructor (2.3)

**Files:** `_auth.py`, `_client.py` (touch the state), docs `auth.md`, `testing.md`; tests.

**Interfaces:**
- `refresh_margin: ClassVar[float] = 120.0`, refused at class definition unless an `int` or `float` (not `bool`),
  finite and not negative: `f"{cls.__name__}.refresh_margin takes the seconds before expiry to refresh, a number of 0 or more (got {value!r})"`.
- A private `_TokenState` dataclass (clock, held token, lock, attempts, minting, failed, identity) behind a
  `functools.cached_property`; `CachedTokenAuth` defines no `__init__`. `timing=` and `refresh_margin=` keywords go.

- [ ] **Tests:** `test_a_subclass_that_skips_super_init_works` (port of E14's `NoSuper`);
  `test_refresh_margin_is_a_class_attribute` (a subclass with `refresh_margin = 10.0` re-mints 10 s before expiry);
  `test_a_bad_refresh_margin_is_refused` (`-1.0`, `True`, `math.nan`, `"5"`); Review Focus 1.
- [ ] Implement, snapshot, changelog **Changed**, commit.

### Task 5: The flow is not a hook (2.4)

**Files:** `_auth.py`; `docs/auth.md` (per-request header → middleware); tests.

- [ ] **Tests:** `test_overriding_the_flow_is_refused` (port of `ext_override_flow.py`), for both bases and for
  `reject_credential_rotation`; a ty probe that reports `override-of-final-method`.
- [ ] Implement (`@final` plus the definition-time check), changelog **Changed**, commit.

### Task 6: Signing reads the client's clock (2.5)

**Files:** `_auth.py` (`sign(self, request: httpx2.Request, now: float) -> None`), `_middleware.py`
(`MiddlewareTransport(..., wall_clock: Clock)`), `_stack.py` (passes `timing.wall_clock`); docs `auth.md`,
`configuration.md`; every `def sign`.

- [ ] **Tests:** `test_sign_receives_the_test_clock` (under `TestClock(epoch=...)`, `now` equals `clock.wall_clock()`,
  and advances across a retry; Review Focus 5).
- [ ] Implement; drop the docs' `time.time()`. Snapshot, changelog **Changed**, commit.

### Task 7: Hooks have the right color (2.6)

**Files:** `_auth.py`, `_options.py` (`check_middleware`, new `check_observers`), `_client.py`; tests.

- [ ] **Tests:** `test_an_async_sign_is_refused` (port of T13), `test_an_async_notify_is_refused`,
  `test_a_sync_middleware_is_refused`, Review Focus 4.
- [ ] Implement, changelog **Changed**, commit.

### Task 8: Constructors read as what they take

**Files:** `_auth.py` (`Auth.__init__(self) -> None`), `docs/auth.md` digest recipe, `tests/auth/test_auth_base.py`.

- [ ] **Test:** `test_constructor_signatures`: `str(inspect.signature(c))` is `"() -> None"` for `Auth`,
  `SigningAuth`, `CachedTokenAuth`, and their own for `StaticHeaderAuth`, `BearerAuth`, `QueryParamAuth`.
- [ ] Implement, snapshot (check the three show `()`), changelog, commit.

### Task 9: Verify

- [ ] Every gate from scratch, the docs build, and
  `grep -rn "signed_headers\|signed_params\|token_headers\|token_params\|mint_token\|mint_flow\|time.time()" docs README.md examples src tests`
  shows only the changelog and specs.
