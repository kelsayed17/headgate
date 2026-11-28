# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""``docs/imports.md`` lists every public name and the module it is imported from, as the modules' ``__all__`` say.

The page is generated from ``__all__``: when a name is added, moved or removed, write it again with
``uv run python -m tests.examples.test_imports_page``, and review the diff.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Final

from tests.package.test_api_surface import _PUBLIC_MODULES

_PAGE: Final = Path(__file__).resolve().parents[2] / "docs" / "imports.md"

_INTRO: Final = """\
# Imports

Every name headgate exports, and the module to import it from. A name has one home, and a traceback or a `repr` names
it by that home: `from headgate.errors import ClientError` raises `ImportError`, since `ClientError` lives in
`headgate`. The two codec flavors, `headgate.msgspec` and `headgate.pydantic`, export the same shortcuts, so switching
flavor is changing one import. This page is generated from each module's `__all__`, and a test keeps it in step.

| Name | Import |
| --- | --- |
"""


def _render() -> str:
    """Return the page: a row per exported name, sorted by name (ignoring case), then by module."""
    rows = sorted(
        (
            (name, module_name)
            for module_name in _PUBLIC_MODULES
            for name in importlib.import_module(module_name).__all__
        ),
        key=lambda row: (row[0].lower(), *row),
    )
    return _INTRO + "".join(f"| `{name}` | `from {module} import {name}` |\n" for name, module in rows)


def test_the_imports_page_lists_every_public_name() -> None:
    """Hold the page to the modules' ``__all__``, so it lists every export where it is imported from, and no other."""
    assert _PAGE.read_text(encoding="utf-8") == _render(), (
        "docs/imports.md is out of step with __all__: run uv run python -m tests.examples.test_imports_page"
    )


if __name__ == "__main__":
    _PAGE.write_text(_render(), encoding="utf-8")
