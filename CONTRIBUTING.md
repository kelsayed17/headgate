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
uv run python -m pytest --cov
```

- **Lint and types are strict.** ruff selects every rule and ty treats every rule as an error. Fix the cause of a
  finding: suppression comments (`noqa`, `ty: ignore`, `pragma: no cover`) are not accepted.
- **Coverage is 100% of lines and branches.** A new branch needs a test; an unreachable branch should be deleted.
- **Docstrings** describe what the code does now and why, in the present tense.

## The retry golden table

`tests/support/retry_golden.json` records how httpx-retries 0.6.0 decided every case in the retry matrix, and
`tests/transport/test_retry_golden.py` replays it against headgate's own retry transport. Never regenerate the table
to make a failing replay pass. A deliberate change to retry behaviour updates the table on purpose, with the row diff
reviewed, in the same pull request.

## Pull requests

Keep a pull request to one change, describe the behaviour it changes, and add an entry to `CHANGELOG.md` under
"Unreleased".
