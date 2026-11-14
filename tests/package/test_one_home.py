# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""Every public name has one home: no object is listed in the ``__all__`` of two public modules.

A public module is one whose dotted path has no part starting with ``_``, at any depth, so a public submodule of a
subpackage counts too. Objects are compared, not names: ``headgate.msgspec.reader`` and ``headgate.pydantic.reader``
share a name but are two readers.
"""

from __future__ import annotations

import dataclasses
import importlib
import pkgutil
import re
from collections import defaultdict
from pathlib import Path

import pytest

import headgate
import headgate.errors
import headgate.testing
from tests.package.test_api_surface import _PUBLIC_MODULES


def _public_modules() -> list[str]:
    """Return every public module of headgate, the package first."""
    found = pkgutil.walk_packages(headgate.__path__, prefix="headgate.")
    names = [info.name for info in found if not any(part.startswith("_") for part in info.name.split("."))]
    return ["headgate", *sorted(names)]


def test_no_object_is_exported_from_two_public_modules() -> None:
    """List each exported object once, naming every module that exports it when it is not."""
    assert set(_public_modules()) == set(_PUBLIC_MODULES), "the walk finds the modules the API snapshot covers"
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


@pytest.mark.parametrize(
    "name",
    ["ClientError", "TransportError", "ClientTimeoutError", "UpstreamStatusError", "AuthError", "RateLimitError"],
)
def test_a_top_level_error_is_defined_in_errors_but_exported_from_the_top_level(name: str) -> None:
    """Keep the error importable from ``headgate.errors``, its defining module, but list it in ``headgate`` only."""
    assert getattr(headgate.errors, name) is getattr(headgate, name)
    assert name not in headgate.errors.__all__
    assert name in headgate.__all__


def test_the_errors_guide_names_each_errors_one_home() -> None:
    """List every exported error class in the errors guide's import table, beside its module."""
    guide = (Path(__file__).parents[2] / "docs" / "errors.md").read_text()
    listed = sorted(re.findall(r"^\| `(\w+)` \| `(headgate(?:\.\w+)?)` \|$", guide, re.MULTILINE))
    modules = [importlib.import_module(module_name) for module_name in _public_modules()]
    exported = sorted(
        (name, module.__name__)
        for module in modules
        for name in module.__all__
        if isinstance(error := getattr(module, name), type) and issubclass(error, Exception)
    )

    assert listed == exported


def test_the_constants_left_the_top_level() -> None:
    """Hold the default timeouts on ``Timeouts`` and keep the mint cap private."""
    assert not hasattr(headgate, "DEFAULT_TIMEOUTS")
    assert not hasattr(headgate, "MAX_MINT_RESPONSE_BYTES")


def test_the_default_timeouts_are_a_class_attribute_not_a_field() -> None:
    """Resolve an unset phase against ``Timeouts.DEFAULT``, which ``replace`` copies rather than changes."""
    resolved = headgate.Timeouts(read=60.0).over(headgate.Timeouts.DEFAULT)
    shorter = dataclasses.replace(headgate.Timeouts.DEFAULT, read=5.0)

    assert (resolved.connect, resolved.read, resolved.write, resolved.pool) == (10.0, 60.0, 10.0, 15.0)
    assert "DEFAULT" not in {field.name for field in dataclasses.fields(headgate.Timeouts)}
    assert (shorter.read, headgate.Timeouts.DEFAULT.read) == (5.0, 30.0)


def test_the_fault_steps_live_in_the_testing_kit() -> None:
    """Export every fault step from ``headgate.testing``, whose fault module is private now."""
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("headgate.testing.faults")
    faults = importlib.import_module("headgate.testing._faults")
    assert set(faults.__all__) <= set(headgate.testing.__all__)
