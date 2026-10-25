## What changes

<!-- The behaviour this changes, for a user of headgate. -->

## Checklist

- [ ] `uv run ruff check`, `uv run ruff format --check` and `uv run ty check` pass, with no suppression comments
- [ ] `uv run python -m pytest --cov` passes at 100% line and branch coverage
- [ ] Docs (README / docs/) and docstrings describe the new behaviour
- [ ] `CHANGELOG.md` has an entry under "Unreleased"
- [ ] A change to the public API is reflected in the API-surface snapshot (`--snapshot-update`, diff reviewed)
