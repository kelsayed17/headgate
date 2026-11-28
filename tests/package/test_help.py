# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""What ``help()`` shows names only what a user can import: no public docstring cites a private name.

The docstrings read are a public module's, each export's, and those of the public members (methods, properties, and
the dunder methods ``help()`` lists) of each exported class and of the headgate classes it derives from. None may name
a private module (``headgate._failures``, ``headgate.security._logging``) anywhere, nor cite, in a cross-reference
(``:func:`...```), a private name: one starting with an underscore, a module-level name headgate defines that no public
module exports (``hold_timing``, ``SYSTEM_TIMING``), or a dotted ``headgate`` name that is not one of a public
module's exports. Such a name is cited by its public home, or its value is stated in prose.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import re
from pathlib import Path
from typing import TYPE_CHECKING, Final

import pytest

import headgate
from tests.examples.test_docs import _resolves
from tests.package.test_api_surface import _PUBLIC_MODULES

if TYPE_CHECKING:
    from collections.abc import Iterator

_SRC: Final = Path(headgate.__file__).parent

#: A cross-reference's target, the explicit one of ``title <target>`` when it has one.
_ROLE: Final = re.compile(
    r":(?:func|data|class|meth|attr|mod|exc|obj|const):`~?(?:[^`<]*<(?P<explicit>[^>`]+)>|(?P<plain>[^`]+))`"
)

#: A dotted name in headgate, wherever a docstring spells one.
_DOTTED: Final = re.compile(r"\bheadgate(?:\.\w+)+")


def _exported() -> set[str]:
    """Return every name a public module exports."""
    return {name for module in _PUBLIC_MODULES for name in importlib.import_module(module).__all__}


def _defined(tree: ast.Module) -> Iterator[str]:
    """Yield the names *tree* defines at its top level: its classes, functions, constants and type aliases."""
    for node in tree.body:
        if isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            yield node.name
        elif isinstance(node, ast.TypeAlias):
            yield node.name.id
        elif isinstance(node, ast.Assign | ast.AnnAssign):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            yield from (target.id for target in targets if isinstance(target, ast.Name))


#: The module-level names headgate defines that no public module exports.
_PRIVATE_NAMES: Final = {
    name for path in _SRC.rglob("*.py") for name in _defined(ast.parse(path.read_text(encoding="utf-8")))
} - _exported()


def _documented(member: object) -> object:
    """Return what holds *member*'s docstring: a property's getter, a class or static method's function."""
    if isinstance(member, property):
        return member.fget
    if isinstance(member, classmethod | staticmethod):
        return member.__func__
    return member


def _docstrings() -> Iterator[tuple[str, str]]:
    """Yield each docstring ``help()`` shows of the public surface, with where it is."""
    for module_name in _PUBLIC_MODULES:
        module = importlib.import_module(module_name)
        yield module_name, inspect.getdoc(module) or ""
        for name in module.__all__:
            export = getattr(module, name)
            if not (isinstance(export, type) or inspect.isfunction(export)):
                continue
            yield f"{module_name}.{name}", inspect.getdoc(export) or ""
            if not isinstance(export, type):
                continue
            for klass in export.__mro__:
                if not klass.__module__.startswith("headgate"):
                    continue
                for member_name, member in vars(klass).items():
                    private = member_name.startswith("_") and not member_name.endswith("__")
                    target = _documented(member)
                    if private or not (callable(target) or isinstance(member, property)):
                        continue
                    doc = getattr(target, "__doc__", None)
                    if isinstance(doc, str):
                        yield f"{module_name}.{name}.{member_name}", inspect.cleandoc(doc)


def _private(parts: list[str]) -> bool:
    """Return whether a dotted name of *parts* has a private part: an underscore first, a dunder apart."""
    return any(part.startswith("_") and not part.endswith("__") for part in parts)


def _private_citations(doc: str) -> list[str]:
    """Return what *doc* names that is private: a private module anywhere, or a private name it cross-references."""
    found = [name for name in _DOTTED.findall(doc) if _private(name.split("."))]
    for match in _ROLE.finditer(doc):
        target = (match["explicit"] or match["plain"]).strip().removesuffix("()")
        parts = target.split(".")
        if _private(parts) or parts[0] in _PRIVATE_NAMES or (parts[0] == "headgate" and not _resolves(target)):
            found.append(target)
    return list(dict.fromkeys(found))


_DOCSTRINGS: Final = list(_docstrings())


def test_the_docstrings_are_read() -> None:
    """Read docstrings of modules, exports and members, so an emptied walk cannot pass the check below unseen."""
    where = {place for place, _ in _DOCSTRINGS}
    assert {"headgate", "headgate.Client", "headgate.Client.get", "headgate.CachedTokenAuth.seed"} <= where


def test_no_public_docstring_cites_a_private_name() -> None:
    """Cite every name by its public home, or state its value in prose, so ``help()`` names nothing private."""
    cited = [f"{where}: {name}" for where, doc in _DOCSTRINGS for name in _private_citations(doc)]
    assert cited == []


@pytest.mark.parametrize(
    ("doc", "found"),
    [
        (":func:`~headgate._failures.mint_error`", ["headgate._failures.mint_error"]),
        ("see :data:`SYSTEM_TIMING`", ["SYSTEM_TIMING"]),
        ("see :func:`hold_timing`", ["hold_timing"]),
        ("see :func:`_check_margin`", ["_check_margin"]),
        ("logs through headgate.security._logging.", ["headgate.security._logging"]),
        (":data:`~headgate.security.CHAIN_ORIGIN`", ["headgate.security.CHAIN_ORIGIN"]),
        (":meth:`the client <headgate.Client.get>`", []),
        (":meth:`ClientError.__reduce__`, :attr:`now`, :class:`httpx2.Response`, :mod:`headgate.timing`", []),
    ],
    ids=["private-module", "constant", "function", "underscore", "prose", "not-exported", "explicit", "public"],
)
def test_a_private_citation_is_found(doc: str, found: list[str]) -> None:
    """Find a private module in prose and in a cross-reference, and a private name cited; pass public ones."""
    assert _private_citations(doc) == found


def test_the_private_names_are_headgates_own() -> None:
    """Hold the names headgate defines and does not export, and none that it exports or imports."""
    assert {"hold_timing", "SYSTEM_TIMING", "AUTH_HOP"} <= _PRIVATE_NAMES
    assert not {"Client", "current_timing", "httpx2", "Final"} & _PRIVATE_NAMES
