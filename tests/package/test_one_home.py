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
import inspect
import pickle
import pkgutil
import re
import sys
import traceback
import types
import typing
from collections import defaultdict
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from string.templatelib import Template

import httpx2
import msgspec
import pytest

import headgate
import headgate.errors
import headgate.retry
import headgate.testing
from headgate._core._homes import HOMES
from tests.package.test_api_surface import _PUBLIC_MODULES

if typing.TYPE_CHECKING:
    from collections.abc import Iterator


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
    assert name not in vars(headgate)
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
    """Hold no such attribute: asking the subpackage that re-exported the name for it names its one home."""
    module = importlib.import_module(module_name)
    assert name not in vars(module)
    assert name not in module.__all__
    with pytest.raises(AttributeError, match=rf"^{name} lives in headgate: from headgate import {name}$"):
        getattr(module, name)


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
    assert name not in vars(headgate.errors)
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-c", f"from headgate.errors import {name}", stderr=asyncio.subprocess.PIPE
    )
    _, stderr = await process.communicate()
    assert process.returncode != 0
    assert f"ImportError: {name} lives in headgate: from headgate import {name}" in stderr.decode()


@pytest.mark.parametrize(
    ("module_name", "name", "hint"),
    [
        ("headgate.retry", "RetryPolicy", "RetryPolicy lives in headgate: from headgate import RetryPolicy"),
        ("headgate", "Backoff", "Backoff lives in headgate.retry: from headgate.retry import Backoff"),
        ("headgate", "Respond", "Respond lives in headgate.testing: from headgate.testing import Respond"),
        (
            "headgate.security",
            "SecurityOptions",
            "SecurityOptions lives in headgate: from headgate import SecurityOptions",
        ),
        (
            "headgate",
            "reader",
            (
                "reader lives in headgate.msgspec and headgate.pydantic: from headgate.msgspec import reader, or from "
                "headgate.pydantic import reader"
            ),
        ),
    ],
    ids=["top-level", "subpackage", "testing", "options", "two-homes"],
)
def test_a_name_asked_of_the_wrong_public_module_names_its_home(
    module_name: str, name: str, hint: str, tmp_path: Path
) -> None:
    """Name the public module, or modules, that export a name another one is asked for.

    Under ``from module import name`` it is an ``ImportError``, since the import statement drops an
    ``AttributeError``'s message; asked as an attribute, it is the ``AttributeError`` a missing attribute raises, so
    ``hasattr`` and ``getattr`` with a default answer as ever.
    """
    module = importlib.import_module(module_name)
    importer = tmp_path / "misplaced.py"
    importer.write_text(f"from {module_name} import {name}\n", encoding="utf-8")
    spec = spec_from_file_location("misplaced", importer)
    assert spec is not None
    assert spec.loader is not None
    with pytest.raises(ImportError) as imported:
        spec.loader.exec_module(module_from_spec(spec))
    with pytest.raises(AttributeError) as asked:
        getattr(module, name)

    assert str(imported.value) == str(asked.value) == hint
    assert (imported.value.name, imported.value.name_from) == (module_name, name)
    assert not hasattr(module, name)
    assert getattr(module, name, None) is None


@pytest.mark.parametrize("module_name", _public_modules())
def test_a_name_no_public_module_exports_is_the_usual_missing_attribute(module_name: str) -> None:
    """Raise the usual ``AttributeError`` for a name nothing exports, and a private or dunder one: ``hasattr`` holds."""
    module = importlib.import_module(module_name)
    for name in ("NoSuchName", "_no_such_name", "__path__" if not hasattr(module, "__path__") else "__nope__"):
        with pytest.raises(AttributeError, match=rf"^module {module_name!r} has no attribute {name!r}$"):
            getattr(module, name)
        assert not hasattr(module, name)


def test_the_homes_the_hints_name_are_every_public_modules_exports() -> None:
    """Hold the written-out homes the hints name to every public module's ``__all__``, so no export goes unhinted."""
    homes: dict[str, list[str]] = {}
    for module_name in _public_modules():
        for export in importlib.import_module(module_name).__all__:
            homes.setdefault(export, []).append(module_name)
    expected = {export: tuple(modules) for export, modules in homes.items()}

    assert dict(HOMES) == expected, f"write HOMES in src/headgate/_core/_homes.py as {expected!r}"


async def test_a_misplaced_name_imports_nothing() -> None:
    """Name a misplaced name's home without importing a module: no codec flavor, OpenTelemetry or curl_cffi."""
    code = (
        "import sys\nimport headgate.retry\nassert not hasattr(headgate.retry, 'RetryPolicy')\n"
        "print(sorted(name for name in ('msgspec', 'pydantic', 'opentelemetry', 'curl_cffi') if name in sys.modules))"
    )
    process = await asyncio.create_subprocess_exec(sys.executable, "-c", code, stdout=asyncio.subprocess.PIPE)
    stdout, _ = await process.communicate()

    assert process.returncode == 0
    assert stdout.decode().strip() == "[]"


async def test_importing_a_submodule_from_its_package_reads_no_other_module() -> None:
    """Import a subpackage by ``from headgate import ...`` without reading every module's exports, which imports them.

    ``from headgate import testing`` asks ``headgate`` for ``testing`` before it imports it, and the answer must not
    import the codec flavors, as reading where a name lives would.
    """
    code = (
        "import sys\nfrom headgate import testing\n"
        "print(sorted(name for name in ('msgspec', 'pydantic') if name in sys.modules))"
    )
    process = await asyncio.create_subprocess_exec(sys.executable, "-c", code, stdout=asyncio.subprocess.PIPE)
    stdout, _ = await process.communicate()

    assert stdout.decode().strip() == "[]"


@pytest.mark.parametrize("module_name", _public_modules())
def test_every_export_reports_its_home(module_name: str) -> None:
    """Report, in ``__module__``, the public module every exported class and function is imported from."""
    module = importlib.import_module(module_name)
    exported = [getattr(module, name) for name in module.__all__]
    homes = {value.__qualname__: value.__module__ for value in exported if isinstance(value, type | types.FunctionType)}

    assert homes == dict.fromkeys(homes, module_name)


@pytest.mark.parametrize("module_name", _public_modules())
def test_no_exported_class_shows_another_objects_source(module_name: str) -> None:
    """Refuse ``inspect.getsource`` for a rehomed class, rather than return lines of its public module (spec 3.6).

    ``inspect`` reads a class's file from its ``__module__`` and its line from its ``__firstlineno__``, which is a line
    of the private module that defines it, so the two together named the public module's lines.
    """
    module = importlib.import_module(module_name)
    shown = {}
    for name in module.__all__:
        value = getattr(module, name)
        if isinstance(value, type):
            try:
                shown[name] = inspect.getsource(value).splitlines()[0]
            except OSError:
                continue
    assert not shown


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


def test_the_types_public_signatures_use_are_exported() -> None:
    """Export, beside ``Method``, ``Raw`` and ``Multipart``, the types their signatures name, so a wrapper can too."""
    assert {"CallUrl", "RawContent", "UploadFile", "UploadFiles"} <= set(headgate.__all__)
    assert isinstance(headgate.RawContent, typing.TypeAliasType)
    assert isinstance(headgate.UploadFile, typing.TypeAliasType)
    assert isinstance(headgate.UploadFiles, typing.TypeAliasType)


def test_call_url_stays_a_plain_union() -> None:
    """Keep ``CallUrl`` an implicit alias, so a type checker spells out ``LiteralString`` where it refuses a ``str``."""
    assert not isinstance(headgate.CallUrl, typing.TypeAliasType)
    assert typing.get_args(headgate.CallUrl) == (typing.LiteralString, Template, httpx2.URL)


def test_raw_content_names_only_what_can_be_imported() -> None:
    """Spell ``RawContent``'s factory out, rather than name a private alias a reader cannot import."""
    assert "BodyFactory" not in repr(headgate.RawContent.__value__)


def _exported() -> set[int]:
    """Return the ids of every object a public module exports."""
    found: set[int] = set()
    for name in _public_modules():
        module = importlib.import_module(name)
        found.update(id(getattr(module, export)) for export in module.__all__)
    return found


def _unexported_in(hint: object, exported: set[int], depth: int = 0) -> Iterator[str]:
    """Yield each headgate class or type alias *hint* names, at any depth, that no public module exports."""
    if depth > _MAX_HINT_DEPTH:
        return
    if isinstance(hint, typing.TypeAliasType | type):
        name = getattr(hint, "__qualname__", hint.__name__)
        if (hint.__module__ or "").startswith("headgate") and id(hint) not in exported:
            yield f"{hint.__module__}.{name}"
        if isinstance(hint, typing.TypeAliasType):
            yield from _unexported_in(hint.__value__, exported, depth + 1)
        return
    origin = typing.get_origin(hint)
    for part in (*typing.get_args(hint), *(() if origin is None or origin is hint else (origin,))):
        yield from _unexported_in(part, exported, depth + 1)


def _public_hints() -> Iterator[tuple[str, object]]:
    """Yield every type a public signature, field or exported alias names, with where it is named."""
    for module_name in _public_modules():
        module = importlib.import_module(module_name)
        for export in module.__all__:
            value = getattr(module, export)
            where = f"{module_name}.{export}"
            if isinstance(value, typing.TypeAliasType):
                yield where, value.__value__
                continue
            members = [value, *(vars(value).items() if isinstance(value, type) else ())]
            for member in members:
                name, func = member if isinstance(member, tuple) else ("", member)
                func = func.fget if isinstance(func, property) else func
                if name.startswith("_") and name not in {"", "__init__", "__call__"}:
                    continue
                if isinstance(func, type | types.FunctionType):
                    for key, hint in typing.get_type_hints(func).items():
                        yield f"{where}.{name or ''}:{key}", hint


#: How deep a type expression is walked, past what any public signature nests.
_MAX_HINT_DEPTH = 8


def test_every_public_signature_names_only_exported_types() -> None:
    """Name only exported headgate types in every public signature, field and alias, so a wrapper can write them."""
    exported = _exported()
    unexported = sorted(
        {f"{found} <- {where}" for where, hint in _public_hints() for found in _unexported_in(hint, exported)}
    )

    assert not unexported


@pytest.mark.parametrize(
    ("cls", "instead"),
    [
        (headgate.retry.Backoff, "factor and max_wait"),
        (headgate.RetryPolicy, "attempts, methods, statuses, backoff"),
        (headgate.HttpCache, "methods, paths and statuses"),
        (headgate.testing.Respond, "status_code, headers, json, content and retry_after"),
    ],
    ids=lambda value: value.__name__ if isinstance(value, type) else "fields",
)
def test_a_value_whose_methods_went_private_cannot_be_subclassed(cls: type, instead: str) -> None:
    """Refuse a subclass whose override the stack would never call, naming the fields to set instead."""
    assert getattr(cls, "__final__", False)
    with pytest.raises(TypeError, match=rf"^Custom cannot subclass {cls.__name__}, which is final: set {instead}"):
        type("Custom", (typing.cast("typing.Any", cls),), {})


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


async def test_policy_disabled_lives_in_the_testing_kit() -> None:
    """Report ``headgate.testing`` as the switch's home once that alone is imported; ``headgate.policy`` is gone."""
    code = (
        "import importlib.util, headgate.testing\n"
        "print(headgate.testing.policy_disabled.__module__, importlib.util.find_spec('headgate.policy'))"
    )
    process = await asyncio.create_subprocess_exec(sys.executable, "-c", code, stdout=asyncio.subprocess.PIPE)
    stdout, _ = await process.communicate()

    assert stdout.decode().split() == ["headgate.testing", "None"]


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
#: module exports, and of each function a class defines, and the value of every type alias it exports, and prints one
#: line per one that does not resolve.
_HINTS = """
import importlib, sys, types, typing
import headgate
failed = []
for name in sys.argv[1:]:
    module = importlib.import_module(name)
    for export in module.__all__:
        value = getattr(module, export)
        if isinstance(value, typing.TypeAliasType):
            try:
                value.__value__
            except Exception as error:
                failed.append(f"{name}.{export}.__value__: {type(error).__name__}: {error}")
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
    """Resolve ``typing.get_type_hints`` of every exported class and function, and every exported alias's value.

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
