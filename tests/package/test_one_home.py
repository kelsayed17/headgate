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
import re
from collections import defaultdict
from pathlib import Path

import pytest

import headgate
import headgate.errors


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


@pytest.mark.parametrize(
    ("module_name", "name"),
    [
        ("headgate.retry", "RetryPolicy"),
        ("headgate.ratelimit", "Rate"),
        ("headgate.ratelimit", "RateBudget"),
        ("headgate.breaker", "BreakerPolicy"),
        ("headgate.cache", "HttpCache"),
        ("headgate.pagination", "Paginated"),
    ],
)
def test_a_top_level_name_is_not_re_exported_by_its_subpackage(module_name: str, name: str) -> None:
    """Hold no such attribute, so importing the name from the subpackage that re-exported it raises ``ImportError``."""
    module = importlib.import_module(module_name)
    assert not hasattr(module, name)
    assert name not in module.__all__


@pytest.mark.parametrize("name", ["ClientError", "TransportError", "UpstreamStatusError", "RateLimitError"])
def test_a_top_level_error_is_defined_in_errors_but_exported_from_the_top_level(name: str) -> None:
    """Keep the error importable from ``headgate.errors``, its defining module, but list it in ``headgate`` only."""
    assert getattr(headgate.errors, name) is getattr(headgate, name)
    assert name not in headgate.errors.__all__
    assert name in headgate.__all__


def test_the_errors_guide_names_each_errors_one_home() -> None:
    """List every exported error class in the errors guide's import table, beside its module."""
    guide = (Path(__file__).parents[2] / "docs" / "errors.md").read_text()
    listed = dict(re.findall(r"^\| `(\w+)` \| `(headgate(?:\.errors)?)` \|$", guide, re.MULTILINE))
    exported = {
        name: module.__name__
        for module in (headgate, headgate.errors)
        for name in module.__all__
        if isinstance(error := getattr(module, name), type) and issubclass(error, Exception)
    }

    assert listed == exported
