# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""Every public name has one home: no object is listed in the ``__all__`` of two public modules.

A public module is one whose dotted path has no part starting with ``_``, at any depth, so a public submodule of a
subpackage counts too. Objects are compared, not names: ``headgate.msgspec.reader`` and ``headgate.pydantic.reader``
share a name but are two readers.
"""

from __future__ import annotations

import asyncio
import copy
import dataclasses
import importlib
import pickle
import pkgutil
import re
import sys
import traceback
import types
from collections import defaultdict
from pathlib import Path

import msgspec
import pytest

import headgate
import headgate.errors
import headgate.retry
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


_TOP_LEVEL_ERRORS = [
    "ClientError",
    "TransportError",
    "ClientTimeoutError",
    "UpstreamStatusError",
    "AuthError",
    "RateLimitError",
]


@pytest.mark.parametrize("name", _TOP_LEVEL_ERRORS)
async def test_a_top_level_error_has_one_home(name: str) -> None:
    """Export the error from ``headgate`` only: importing it from ``headgate.errors`` raises ``ImportError``."""
    assert name in headgate.__all__
    assert name not in headgate.errors.__all__
    assert not hasattr(headgate.errors, name)
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-c", f"from headgate.errors import {name}", stderr=asyncio.subprocess.PIPE
    )
    _, stderr = await process.communicate()
    assert process.returncode != 0
    assert f"ImportError: cannot import name '{name}' from 'headgate.errors'" in stderr.decode()


@pytest.mark.parametrize("module_name", _public_modules())
def test_every_export_reports_its_home(module_name: str) -> None:
    """Report, in ``__module__``, the public module every exported class and function is imported from."""
    module = importlib.import_module(module_name)
    exported = [getattr(module, name) for name in module.__all__]
    homes = {value.__qualname__: value.__module__ for value in exported if isinstance(value, type | types.FunctionType)}

    assert homes == dict.fromkeys(homes, module_name)


def test_a_traceback_names_the_public_home() -> None:
    """Print ``headgate.UpstreamStatusError`` and ``headgate.testing.UnmatchedRequestError``, what a user imports."""
    status = headgate.UpstreamStatusError("boom", status_code=500)
    unmatched = headgate.testing.UnmatchedRequestError("no reply for GET /x")

    assert "".join(traceback.format_exception(status)).startswith("headgate.UpstreamStatusError: ")
    assert "".join(traceback.format_exception(unmatched)).startswith("headgate.testing.UnmatchedRequestError: ")


@pytest.mark.parametrize("name", _TOP_LEVEL_ERRORS)
def test_a_top_level_error_pickles_by_its_public_home(name: str) -> None:
    """Pickle the class by the home it reports, which ``pickle`` refuses unless that name is the class."""
    kind = getattr(headgate, name)
    error = kind("boom", status_code=503) if issubclass(kind, headgate.UpstreamStatusError) else kind("boom")

    assert f"headgate\n{name}".encode() in pickle.dumps(kind, protocol=0)
    assert type(copy.deepcopy(error)) is kind


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


async def test_what_a_client_hands_out_reports_its_home_after_a_bare_import() -> None:
    """Name the public homes of the events, errors and timing a client hands out, with only ``headgate`` imported.

    An observer receives events, and a caller errors, from a client whose module may be all the program imported.
    """
    code = (
        "import headgate, headgate._core as core, headgate._errors as errors\n"
        "print(core.CallStarted.__module__, core.CallView.__module__, core.Timing.__module__,"
        " errors.CircuitOpenError.__module__, errors.UpstreamStatusError.__module__)"
    )
    process = await asyncio.create_subprocess_exec(sys.executable, "-c", code, stdout=asyncio.subprocess.PIPE)
    stdout, _ = await process.communicate()

    assert stdout.decode().split() == [
        "headgate.events",
        "headgate.events",
        "headgate.timing",
        "headgate.errors",
        "headgate",
    ]


#: A script that resolves, after a bare ``import headgate``, the type hints of every class and function each public
#: module exports, and of each function a class defines, and prints one line per one whose hints do not resolve.
_HINTS = """
import importlib, sys, types, typing
import headgate
failed = []
for name in sys.argv[1:]:
    module = importlib.import_module(name)
    for export in module.__all__:
        value = getattr(module, export)
        if isinstance(value, type | types.FunctionType):
            members = [value, *(vars(value).values() if isinstance(value, type) else ())]
            for member in members:
                if not isinstance(member, type | types.FunctionType):
                    continue
                try:
                    typing.get_type_hints(member)
                except Exception as error:
                    failed.append(f"{name}.{export}.{member.__name__}: {type(error).__name__}: {error}")
print("\\n".join(failed))
"""


async def test_every_exports_type_hints_resolve() -> None:
    """Resolve ``typing.get_type_hints`` of every exported class and function, as msgspec and pydantic read them.

    It runs in a fresh process after a bare ``import headgate``, so a hint is only resolved by the names its own module
    binds at runtime: a name imported only for the type checker would fail here, as it would for a user.
    """
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-c", _HINTS, *_public_modules(), stdout=asyncio.subprocess.PIPE
    )
    stdout, _ = await process.communicate()

    assert stdout.decode().strip() == ""


@pytest.mark.parametrize(
    ("kind", "data"),
    [
        (headgate.Timeouts, {"read": 5.0}),
        (headgate.BreakerPolicy, {"failure_threshold": 3}),
        (headgate.retry.Backoff, {"factor": 1.0}),
        (headgate.retry.RetryShare, {"ratio": 0.1}),
    ],
    ids=["timeouts", "breaker-policy", "backoff", "retry-share"],
)
def test_msgspec_converts_into_an_option(kind: type, data: dict[str, object]) -> None:
    """Build an option from builtins with ``msgspec.convert``, which reads the option's type hints.

    ``RetryPolicy`` resolves its hints too (above), but msgspec refuses its ``budget: RetryShare | RetryBudget | None``
    itself, a union of two classes, so it is not converted here.
    """
    assert msgspec.convert(data, kind) == kind(**data)


@pytest.mark.parametrize("module_name", _public_modules())
def test_a_public_module_binds_only_what_it_exports(module_name: str) -> None:
    """Bind no public name but those in ``__all__`` and the public submodules, so nothing else has a second home."""
    module = importlib.import_module(module_name)
    public = {name: value for name, value in vars(module).items() if not name.startswith("_")}
    submodules = {
        name
        for name, value in public.items()
        if isinstance(value, types.ModuleType) and value.__name__ == f"{module_name}.{name}"
    }

    assert set(public) == set(module.__all__) | submodules
