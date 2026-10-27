# Contributing

## Setup

headgate needs Python 3.14 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
```

## Checks

Every change must pass all of these, which CI runs on every pull request:

```bash
uv run ruff check
uv run ruff format --check
uv run ty check
uv run typos
uv run python -m pytest --cov
```

- **Lint and types are strict.** ruff selects every rule and ty treats every rule as an error. Fix the cause of a
  finding: suppression comments (`noqa`, `ty: ignore`, `pragma: no cover`) are not accepted.
- **Coverage is 100% of lines and branches.** A new branch needs a test; an unreachable branch should be deleted.
- **Docstrings** describe what the code does now and why, in the present tense.
- **American English** in code, docs and messages: `typos` checks the spelling (`behavior`, `normalize`, `license`).

## Retry invariants

`tests/retry/test_properties.py` checks, with Hypothesis over random `FaultyTransport` schedules, the invariants every
change to the retry layer must keep: a POST or PATCH without an idempotency key is never re-sent after any of its bytes
were written (unless the server proved it unprocessed); no attempt or wait runs past the deadline; retries never exceed
the budget; every response that is not returned is closed; and the error's attempt history matches what was sent. A
failing example is a bug to fix, not a test to loosen. Behavior the invariants do not pin is covered by the behavior
tests beside them.

## Pull requests

Keep a pull request to one change, describe the behavior it changes, and add an entry to `CHANGELOG.md` under
"Unreleased".
