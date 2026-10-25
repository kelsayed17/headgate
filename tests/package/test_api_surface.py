# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""The public API surface, as a snapshot: every exported name, signature, dataclass field and default.

The rendering is plain text, one block per public module, so a change to the surface shows as a reviewable diff of
``__snapshots__/test_api_surface.ambr``. Update it only on purpose, with ``--snapshot-update``, in the change that
alters the API. Memory addresses in default values are masked, so the text is the same on every run.
"""

from __future__ import annotations

import annotationlib
import dataclasses
import enum
import importlib
import inspect
import pkgutil
import re
from typing import TYPE_CHECKING, TypeAliasType, is_typeddict

import pytest

if TYPE_CHECKING:
    from collections.abc import Callable

    from syrupy.assertion import SnapshotAssertion

#: Every module whose ``__all__`` is public API, the package root first.
_PUBLIC_MODULES = (
    "headgate",
    "headgate.breaker",
    "headgate.cache",
    "headgate.curl",
    "headgate.errors",
    "headgate.events",
    "headgate.health",
    "headgate.msgspec",
    "headgate.pagination",
    "headgate.otel",
    "headgate.policy",
    "headgate.pydantic",
    "headgate.ratelimit",
    "headgate.retry",
    "headgate.streaming",
    "headgate.testing",
    "headgate.security",
)

#: An object's address in a default value's ``repr``, which differs between runs.
_ADDRESS = re.compile(r" at 0x[0-9a-fA-F]+")

#: A flat set literal in a ``repr``, whose item order follows string hashing and so differs between runs.
_SET = re.compile(r"frozenset\(\{([^{}()]*)\}\)")


def _sorted_set(match: re.Match[str]) -> str:
    """Return the set literal *match* found with its items sorted."""
    return f"frozenset({{{', '.join(sorted(match.group(1).split(', ')))}}})"


def _masked(text: str) -> str:
    """Return *text* with every memory address masked and every flat set's items sorted."""
    return _SET.sub(_sorted_set, _ADDRESS.sub(" at 0x…", text))


def _signature(obj: Callable[..., object]) -> str:
    """Return *obj*'s signature as text, or a marker for a callable that has none."""
    try:
        return _masked(str(inspect.signature(obj, annotation_format=annotationlib.Format.STRING)))
    except TypeError, ValueError:
        return "(no signature)"


def _field_default(field: dataclasses.Field[object]) -> str:
    """Return a dataclass field's default as text: its value, ``<factory>``, or nothing when it is required."""
    if field.default is not dataclasses.MISSING:
        return f" = {_masked(repr(field.default))}"
    if field.default_factory is not dataclasses.MISSING:
        return " = <factory>"
    return ""


def _members(cls: type) -> list[str]:
    """Render the public methods, properties and enum members *cls* defines itself."""
    lines: list[str] = []
    if issubclass(cls, enum.Enum):
        lines.extend(f"    {member.name} = {member.value!r}" for member in cls)
        return lines
    for name, member in sorted(vars(cls).items()):
        if name.startswith("_") and name != "__call__":
            continue
        if isinstance(member, property):
            lines.append(f"    property {name}")
        elif isinstance(member, classmethod | staticmethod):
            lines.append(f"    {type(member).__name__} {name}{_signature(member.__func__)}")
        elif callable(member):
            lines.append(f"    def {name}{_signature(member)}")
    return lines


def _render_class(name: str, cls: type) -> list[str]:
    """Render a class: its bases, constructor, dataclass fields or TypedDict keys, and its own public members."""
    bases = ", ".join(base.__qualname__ for base in cls.__bases__)
    lines = [f"class {name}({bases}){_signature(cls)}"]
    if dataclasses.is_dataclass(cls):
        lines.extend(f"    field {f.name}: {f.type}{_field_default(f)}" for f in dataclasses.fields(cls))
    elif is_typeddict(cls):
        annotations = annotationlib.get_annotations(cls, format=annotationlib.Format.STRING)
        required = getattr(cls, "__required_keys__", frozenset())
        lines.extend(
            f"    key {key}: {kind}{'' if key in required else ' (optional)'}" for key, kind in annotations.items()
        )
    lines.extend(_members(cls))
    return lines


def _render(module_name: str) -> str:
    """Render *module_name*'s public surface, name by name in ``__all__`` order."""
    module = importlib.import_module(module_name)
    names: list[str] = list(module.__all__)
    lines = [f"module {module_name}", f"__all__ = {names}"]
    for name in names:
        obj = getattr(module, name)
        if isinstance(obj, TypeAliasType):
            value = annotationlib.call_evaluate_function(obj.evaluate_value, annotationlib.Format.STRING)
            lines.append(f"type {name}{list(obj.__type_params__) or ''} = {value}")
        elif isinstance(obj, type):
            lines.extend(_render_class(name, obj))
        elif callable(obj):
            lines.append(f"def {name}{_signature(obj)}")
        else:
            lines.append(f"{name} = {_masked(repr(obj))}")
    return "\n".join(lines)


# transport_policy: keep the real ``RateBudget.wait``, which the suite's autouse fixture otherwise patches.
@pytest.mark.transport_policy
@pytest.mark.parametrize("module_name", _PUBLIC_MODULES)
def test_the_public_surface_matches_the_snapshot(module_name: str, snapshot: SnapshotAssertion) -> None:
    """Check that the module's public names, signatures, fields and defaults are exactly the reviewed ones."""
    assert _render(module_name) == snapshot


def test_every_public_module_is_snapshotted() -> None:
    """Check that every public module under ``headgate`` is in the snapshot, so a new one cannot slip past it."""
    package = importlib.import_module("headgate")
    modules = pkgutil.iter_modules(package.__path__)
    found = {f"headgate.{info.name}" for info in modules if not info.name.startswith("_")}
    assert found | {"headgate"} == set(_PUBLIC_MODULES)
