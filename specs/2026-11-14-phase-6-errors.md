# Phase 6: Errors — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make what a `ClientError` says about a failed call true and fixed. `retryable` has one meaning, which covers every way a call can fail. A new `sent` says whether any byte of the request can have reached the network. An error never carries a status code from another call. And no attribute of an error can be reassigned after it is raised.

**Architecture:**
- **`retryable`.** It keeps its rule for a network failure or a status: the retry policy's verdict on the outcome that ended the call. Each refusal that headgate makes itself, before the request leaves, now says `True` with its `retry_after`. Each error that sending again cannot cure now says `False`. A table in `errors.py` states the rule for each class, and a parametrized test pins it.
- **`sent`.** The retry layer records, after every attempt, whether that attempt can have written a byte. It uses the send progress and the exception type, as the retry classifier already does. It records this on the call's `CallContext` once, for the whole call: `True` if any attempt may have written. `CallSite` reads the record into `ErrorDetails`, and `ClientError` exposes it.
- **`status_code=None`.** A pause of headgate's own rate budget that outlasts the deadline raises `RateLimitError` with `status_code=None`, not the status of the earlier response that caused the pause. `UpstreamStatusError.status_code` is typed `int | None`.
- **Read-only.** Every attribute of every error class is a property backed by a private field, `source` and `status_code` included. Pickling is unchanged, because `__reduce__` already restores `__dict__`.

**Tech Stack:** Python 3.14, httpx2 2.16, ty, ruff, pytest, syrupy (the API snapshot and the error-message snapshot).

**Spec:** `specs/2026-11-05-public-api-redesign-design.md`, section 6. Phases 1–4 have merged (#56, #60, #85, #94). The design marks this phase as independent of the others.

## Global Constraints

- Python 3.14 or newer; httpx2 2.16 or newer; hishel 2.
- Base branch: `origin/main` at or after `9379587` (#94).
- Clean break: no deprecation aliases. Code that assigns an error attribute raises `AttributeError`.
- Gates for every task:
  - `uv run ruff check`
  - `uv run ruff format --check`
  - `uv run ty check`
  - `uv run typos`
  - `uv run python -m pytest --cov` at 100% line and branch coverage
  - No `noqa`, `ty: ignore` or `pragma: no cover`.
- A change to the public API updates the API snapshot (`uv run python -m pytest tests/package/test_api_surface.py --snapshot-update`). A change to a message updates the error-message snapshot (`tests/errors/test_error_messages_snapshot.py`). Each diff is reviewed.
- Every exported name appears in `docs/`, and a test enforces it.
- American English in code, docs and messages.
- Each task adds a `CHANGELOG.md` entry under `## [Unreleased]`, in the existing **Added**, **Changed** or **Removed** section, and says what to write instead.
- Docstrings describe what the code does now, in the present tense.
- The property tests in `tests/retry/test_properties.py` keep their properties and assertions unchanged. This phase changes nothing they spell.
- The verb-path benchmarks (`benchmarks/test_client.py`, `benchmarks/test_hot_paths.py`) show no regression against `origin/main`. Recording `sent` adds one attribute write per attempt.
- Out of scope:
  - The `AsyncBaseClient` → `Client` rename and the public `CallInfo` (phase 5).
  - `max_pages=`, which replaces `limit=PageLimit(...)` and so changes `PageLimitError`'s docstring (phase 7). This phase makes `PageLimitError.pages` read-only and nothing more.
  - Moving the error classes between `headgate` and `headgate.errors` (phase 8).
  - Using a closed client: it keeps raising `RuntimeError`. That is pinned by `tests/client/test_client.py` and is deliberate, like the `ValueError` for a bad t-string. This phase adds nothing there.

## Coordination with phase 5

Phase 5 is being planned at the same time (the `hg-phase-3` session). Both phases touch:

- **`errors.py` and `_failures.py`.** Phase 5's rename changes the docstrings that name `AsyncBaseClient`. This phase changes the classes and `CallSite`. The overlap is text, not design.
- **`_client.py`.** This phase changes only the `except` arms of `_exchange`, where the breaker and budget refusals are raised, and the two `_site` helpers. Phase 5 renames the class and changes its construction.
- **`_core/_context.py`.** This phase adds the call's `sent` record to `CallContext`. Phase 5 adds a `call_info()` function beside it, which reads the attempt and the time left. `CallInfo` stays in `_core`.
- **`retry/_transport.py` and `ratelimit/_budget.py`.** This phase changes `_Sequence._send` and drops the budget's `_pause_status`. Phase 5 changes the retry ledger's lookup and `RateBudget.__init__`'s `timing=`. Both are nearby, but neither changes the other's lines.

**Phase 6 lands first.** It is smaller, renames nothing, and adds no public module. Phase 5's rename then applies mechanically over it, last, on top of these tests, and its `call_info()` can decide whether to show `sent` with the field already in place. The phase 5 planner agreed, and its plan (#96) recommends the same order.

## Public signatures after this phase

Everything not shown is unchanged.

```python
# headgate.errors
class ErrorDetails(TypedDict, total=False):
    source: str | None
    method: str | None
    url: str | None
    request_id: str | None
    attempts: int | None
    retryable: bool
    retry_after: float | None
    sent: bool | None  # new


class ClientError(Exception):
    def __init__(self, message: str, **details: Unpack[ErrorDetails]) -> None: ...
    @property
    def source(self) -> str | None: ...  # now read-only
    @property
    def method(self) -> str | None: ...
    @property
    def url(self) -> str | None: ...
    @property
    def request_id(self) -> str | None: ...
    @property
    def attempts(self) -> int | None: ...
    @property
    def retryable(self) -> bool: ...  # one meaning; see Decisions
    @property
    def retry_after(self) -> float | None: ...
    @property
    def sent(self) -> bool | None: ...  # new


class UpstreamStatusError(ClientError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None,  # was int
        code: str | None = None,
        body: str | None = None,
        problem: Problem | None = None,
        **details: Unpack[ErrorDetails],
    ) -> None: ...
    @property
    def status_code(self) -> int | None: ...  # now read-only; None only for the rate budget's own refusal
    @property
    def code(self) -> str | None: ...  # now read-only
    @property
    def problem(self) -> Problem | None: ...


class PageLimitError(ClientError):
    @property
    def pages(self) -> int: ...  # now read-only


class RefusedError(Exception):
    @property
    def message(self) -> str: ...  # now read-only
    @property
    def code(self) -> str | None: ...  # now read-only
    @property
    def kind(self) -> type[UpstreamStatusError]: ...  # now read-only
```

## Migration

| Before | After |
| --- | --- |
| `error.status_code = 503` (or any error attribute assigned) | Raises `AttributeError`. Build a new error with the value instead. |
| `except CircuitOpenError` relied on `retryable is False` | `CircuitOpenError.retryable` is `True`, and `retry_after` is the cooldown left. Use `except CircuitOpenError` to tell it apart. |
| `RateLimitError.status_code` was `429` (or `503`) for the rate budget's own refusal | It is `None`, and `sent` is `False`. A 429 the wire sent for this call still carries `429`. |
| `UpstreamStatusError.status_code: int` | `int | None`. Code that compares it with `<` or `>=` narrows it first. `==`, `in` and `match` need no change. |
| Whether a failed write may have reached the server was read from the notes | Read `error.sent`. `False` means headgate knows no byte left. `True` means some may have. `None` means the error was not raised by a client call. |

## Decisions this plan makes

The design leaves these to the builder or does not settle them. Each is the planner's call, made by the maintainer's rule: the cleanest design, even where it breaks things.

- **`retryable` keeps method safety.** The design's one meaning is "sending this call again later could succeed". This plan reads that as "could succeed and is safe to send again as the call stands".
  - A failed call is safe to send again if its method is idempotent, if it carries an idempotency key, or if none of it was sent.
  - So a surviving 503 on a POST without a key stays `retryable=False`, with `sent=True`. Today's classifier rule is unchanged for statuses and network failures.
  - The alternative, a `retryable` that ignores safety, would make the recipe in `docs/errors.md` (`if exc.retryable: reschedule(...)`) send a write twice. Making a caller combine `retryable` with `sent` to stay safe is the kind of safety-by-spelling the redesign removes.
  - `sent` adds what `retryable` alone cannot say: whether a write that is not retryable may have been processed. That is the answer a caller needs before it reconciles.
- **The rule for each class.** `errors.py`'s module docstring gains this table, and `docs/errors.md` repeats it:

  | Error | `retryable` | `sent` |
  | --- | --- | --- |
  | `UpstreamStatusError`, `AuthError`, `RateLimitError` from a response | the retry policy's verdict on the status | `True` |
  | `RateLimitError` from the rate budget's pause past the deadline | `True`, `retry_after` = the pause left | `False` |
  | `ClientTimeoutError` from the rate budget's turn past the deadline | `True`, `retry_after` = until the turn | `False` |
  | `CircuitOpenError` | `True` (was `False`), `retry_after` = the cooldown left | as recorded: `False`, since the breaker refuses before the first send |
  | `TransportError`, `ClientTimeoutError` from the network or the deadline | the retry policy's verdict | as recorded (below) |
  | `ForbiddenDestinationError` | `False`: the same hop is refused again | as recorded: `False` unless an earlier hop was sent |
  | `ResponseTooLargeError`, `ResponseParseError` | `False`: the same body comes back | `True` |
  | `ClientConfigurationError` | `False` | as recorded |
  | `PageLimitError` | `False` | `None`: no send failed |
  | a `ClientError` your own code raises | what you pass, `False` by default | what you pass, `None` by default |

- **`sent` is `bool | None`, and `True` means "may have".** `None` follows the rule the other attributes already follow: an attribute is `None` when the raiser did not know it. headgate's own call errors always set it.
  - It is `False` only when headgate knows that no byte left. That covers every attempt that obtained a connection and wrote nothing, failed with a `NEVER_SENT_ERRORS` type, was refused by the breaker, the budget or the SSRF guard, or was withdrawn by the rate budget.
  - Otherwise it is `True`. That includes a custom transport that reports no progress and raised anything else, since a wrong `False` is the one answer that could make a caller send a write twice.
  - It covers the whole call: the first send, its retries, its hedges and its followed redirects. It does not cover a `CachedTokenAuth` mint hop. The hop is another request, marked `AUTH_HOP`, and the failure of the call's own request is what the attribute describes.
- **One place records `sent`, and one place reports it.** The retry layer already holds each attempt's result and its `SendProgress` and judges "nothing was sent" for the classifier (now `_classify.nothing_sent`). After every attempt, `_Sequence._send` calls `CallContext.mark_sent()` when that judgment is "something may have been". `CallContext.sent` is the call-level record. `SendProgress` stays per attempt, as #91 made it.
  - Every `ClientError` that leaves a call passes through `_reported`, which already adds the call's notes to it. That is also where it takes the call's `sent`, whichever layer raised it: the network, the breaker, the budget, a token mint, or a middleware.
  - So no error builder threads `sent` through, and a failed mint shared with the calls waiting behind it (#86) still reports each waiting call's own `sent`, because each waiting call raises its own copy.
  - A mint request never touches the call's record, because it is sent with a call context of its own. (Amended while implementing. The plan first had `CallSite` carry `sent`, which would have missed the mint's errors.)
- **`status_code: int | None` on `UpstreamStatusError`, not a new class.** The design wants the budget's refusal to carry `None`. A subclass cannot widen its parent's `int` to `int | None` (ty rejects the override), so the field is widened where it is declared.
  - The alternative was to raise the budget's refusal as a class outside `UpstreamStatusError`. A caller who backs off on `except RateLimitError` would then miss it, and the two refusals that mean "slow down" would need two `except` arms.
  - `None` appears only for that one refusal, which is documented. `status_code` is otherwise never invented, as today.
  - When `status_code` is `None`, the message drops the `(status=...)` tag rather than printing `status=None`.
  - `BudgetPausedError.status_code` (in `_core`, private) is removed, since nothing reads it any more.
- **`RefusedError`'s fields are read-only too.** It is not a `ClientError`, but the design says every error attribute. It is a value a reader returns or raises, and the client reads it once.
- **No change to the error tree.** Every class keeps its parent, so a boundary's mapping (`ClientTimeoutError` to 504, and so on) is unchanged.

## Review Focus

These cases are not covered by the design's own examples and are the most likely to bite users. Each gets a test in the task named.

1. **`sent` on the edges.**
   - A `ConnectError` from the network: `False`.
   - A `ReadTimeout` after the body was written, measured with a loopback server: `True`.
   - A custom transport that reports no progress and raises `ReadError`: `True`. One that raises `ConnectError`: `False`.
   - A GET answered 503, then 200 after a retry, then raising later on a parse error: `True`.
   - A POST refused by an open breaker: `False`, with `attempts == 0`.
   - A call whose `CachedTokenAuth` mint failed before the call's own request was sent: `False`.
   - A 307 whose second hop fails to connect: `True`, because the first hop was sent.

   Covered in Task 3.
2. **`retryable` per class.** One parametrized case per row of the Decisions table, with `retry_after` checked for the three refusals headgate makes itself. Covered in Task 4.
3. **The docs recipe.** The `docs/errors.md` example, run as a test against an open breaker, a budget pause, a surviving GET 503 and a surviving POST 503 without a key. It reschedules the first three and re-raises the last. Covered in Task 4.
4. **`status_code=None`.**
   - The budget pause's `RateLimitError` has `status_code is None`, `sent is False`, `retryable is True` and a message without a status tag.
   - A 429 the wire sent for this call still has `429`.
   - An in-band quota refusal in a 200 body still has `200`.

   Covered in Task 5.
5. **Read-only.**
   - Assigning each attribute of each class raises `AttributeError`.
   - Every error still survives `pickle` and `copy.deepcopy` with every attribute, `sent` included.

   Covered in Task 2.

---

### Task 1: Branch, and commit this plan

**Files:**
- Create: `specs/2026-11-14-phase-6-errors.md` (this plan).
- Modify: `specs/2026-11-05-public-api-redesign-design.md`, section 6: `retryable` keeps method safety, `sent` is `bool | None`, and `status_code` is `int | None`. Mark it "(Amended with the phase 6 plan.)".

- [ ] **Step 1:** Work in `claude/plan-phase-6-errors`, cut from `origin/main` at `9379587`.
- [ ] **Step 2:** Run `uv run typos specs/`. Expected: no output.
- [ ] **Step 3:** Commit: `Add the phase 6 plan of the API redesign`. Open it as a draft PR for review. `src/` stays untouched until the plan is approved.

### Task 2: Every error attribute is read-only

**Files:**
- Modify: `src/headgate/errors.py`: `ClientError.source`, `UpstreamStatusError.status_code` and `.code`, `PageLimitError.pages`, and `RefusedError.message`, `.code` and `.kind` become properties over private fields.
- Modify: every reader of those fields inside `src/` (none assigns them, per `grep -rn "\.\(status_code\|code\|source\|pages\) =" src`).
- Test: `tests/client/test_error_details.py`, `tests/errors/`.

- [ ] **Step 1: Write the failing tests:**
  - A parametrized test assigns each attribute of an instance of each class and expects `AttributeError`.
  - The existing pickle tests extend to `copy.deepcopy`.
- [ ] **Step 2:** Run them. Expected: FAIL on the assignments.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run every gate, and update the API snapshot.
- [ ] **Step 5:** CHANGELOG under **Changed**: every error attribute is read-only; build a new error instead of assigning. Commit: `Make every error attribute read-only`.

### Task 3: `sent`

**Files:**
- Modify: `src/headgate/_core/_context.py`: `CallContext.mark_sent()` and the read-only `CallContext.sent: bool`.
- Modify: `src/headgate/retry/_classify.py`: expose the "nothing was sent" judgment (today `_nothing_sent`) as `nothing_sent(progress, result)`. A response means something was sent.
- Modify: `src/headgate/retry/_transport.py`: `_Sequence._send` marks the context after each attempt, unless the request is an `AUTH_HOP` or the rate budget withdrew the attempt.
- Modify: `src/headgate/_client.py`: `_reported` gives every `ClientError` leaving the call the call's `sent`.
- Modify: `src/headgate/errors.py`: `ErrorDetails.sent`, `ClientError.sent`, and the module docstring's table.
- Modify: `docs/errors.md`: the attribute table gains `sent`, with its three values.
- Test: `tests/client/test_error_details.py`, `tests/retry/test_tracking.py` (for the loopback cases), `tests/auth/test_mint_failures.py`.

- [ ] **Step 1: Write the failing tests**, one per line of Review Focus 1.
- [ ] **Step 2:** Run them. Expected: FAIL, because `ClientError` has no `sent`.
- [ ] **Step 3:** Implement as in **Files**.
- [ ] **Step 4:** Run every gate, update the API snapshot, and run the verb-path benchmarks against `origin/main`.
- [ ] **Step 5:** CHANGELOG under **Added**: `ClientError.sent`. Commit: `Record whether any byte of a failed call can have been sent`.

### Task 4: `retryable` has one meaning

**Files:**
- Modify: `src/headgate/_client.py`: the `BreakerOpenError` arm passes `retryable=True` with the cooldown.
- Modify: `src/headgate/_failures.py`: `ForbiddenDestinationError`, `ResponseTooLargeError`, `ResponseParseError` and `ClientConfigurationError` are built with `retryable=False` explicitly, so a retry verdict left on the context from an earlier attempt cannot leak into them.
- Modify: `src/headgate/errors.py`: the class docstrings and `ClientError.retryable`'s docstring, which state the one meaning.
- Modify: `docs/errors.md` ("Reading a failure", and the attribute table) and `docs/retries.md` ("What `retryable` means"), with the per-class table from Decisions.
- Test: `tests/client/test_error_details.py`, and a new `tests/errors/test_recipe.py` for Review Focus 3.

- [ ] **Step 1: Write the failing tests:** the parametrized table (Review Focus 2) and the recipe (Review Focus 3).
- [ ] **Step 2:** Run them. Expected: FAIL on `CircuitOpenError`.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run every gate.
- [ ] **Step 5:** CHANGELOG under **Changed**: `CircuitOpenError.retryable` is `True`, with `retry_after` the cooldown left; the docs' recipe now reschedules it. Commit: `Give retryable one meaning for every error`.

### Task 5: The rate budget's own refusal carries no status

**Files:**
- Modify: `src/headgate/errors.py`: `UpstreamStatusError(status_code: int | None)`, the read-only `status_code`, and the message without a status tag for `None`.
- Modify: `src/headgate/_client.py`: the `BudgetPausedError` arm passes `status_code=None` and `sent=False`.
- Modify: `src/headgate/_core/_seams.py` and `src/headgate/ratelimit/_budget.py`: `BudgetPausedError` loses `status_code`, and `RateBudget` stops recording `_pause_status` when nothing else reads it.
- Modify: `docs/errors.md` (`status_code`: "never invented; `None` only for the rate budget's own refusal") and `docs/rate-budgets.md`.
- Test: `tests/ratelimit/`, `tests/client/test_error_details.py`, and the error-message snapshot.

- [ ] **Step 1: Write the failing tests** for Review Focus 4.
- [ ] **Step 2:** Run them. Expected: FAIL, because `status_code` is `429`.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run every gate, and update both snapshots.
- [ ] **Step 5:** CHANGELOG under **Changed**: the refusal's `status_code` is `None`, and `UpstreamStatusError.status_code` is `int | None`. Code that orders it narrows it first. Commit: `Give the rate budget's own refusal no status code`.

### Task 6: Verify the phase and open its PR

- [ ] **Step 1:** Rebase onto `origin/main`. Run every gate, the verb-path benchmarks, and `uv run python -m pytest tests/retry/test_properties.py` on its own.
- [ ] **Step 2:** Read the API-snapshot and error-message-snapshot diffs whole. Every changed line is one this plan names.
- [ ] **Step 3:** Open the PR. Its body lists the Migration table and each Review Focus test by name.
