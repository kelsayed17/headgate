# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""Every public name has one home: no object is listed in the ``__all__`` of two public modules.

A public module is one whose dotted path has no part starting with ``_``, at any depth, so a public submodule of a
subpackage counts too. Objects are compared, not names: ``headgate.msgspec.reader`` and ``headgate.pydantic.reader``
share a name but are two readers.
"""

from __future__ import annotations

import importlib
import pkgutil
from collections import defaultdict

import pytest

import headgate


def _public_modules() -> list[str]:
    """Return every public module of headgate, the package first."""
    found = pkgutil.walk_packages(headgate.__path__, prefix="headgate.")
    names = [info.name for info in found if not any(part.startswith("_") for part in info.name.split("."))]
    return ["headgate", *sorted(names)]


@pytest.mark.xfail(strict=True, reason="phase 8's later tasks move the names")
def test_no_object_is_exported_from_two_public_modules() -> None:
    """List each exported object once, naming every module that exports it when it is not."""
    homes: defaultdict[int, list[str]] = defaultdict(list)
    for module_name in _public_modules():
        module = importlib.import_module(module_name)
        for name in module.__all__:
            homes[id(getattr(module, name))].append(f"{module_name}.{name}")

    assert not sorted(where for where in homes.values() if len(where) > 1)


@pytest.mark.parametrize("name", ["Cursor", "LinkHeader", "Offset", "ResponseParseError", "PageLimitError", "Problem"])
def test_the_pagers_and_the_finer_errors_are_not_at_the_top_level(name: str) -> None:
    """Leave a pager to ``headgate.pagination`` and an error outside the six to ``headgate.errors``."""
    assert not hasattr(headgate, name)
    assert name not in headgate.__all__
