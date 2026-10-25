# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""The import boundaries that keep the policy subpackages self-contained, checked by parsing every import.

``headgate.retry``, ``headgate.breaker`` and ``headgate.ratelimit`` may import only the standard library, ``httpx2``
(``ratelimit`` also ``aiolimiter``, the dependency it wraps), their own subpackage and ``headgate._core``; never the
client, the stack, the cache or each other. ``headgate._core`` may import only the standard library, ``httpx2`` and
itself. So any of them could ship as a distribution of its own, and coordination between them has to go through the
core's protocols, wired together in ``headgate._stack``.

Every import counts, those under ``if TYPE_CHECKING:`` included, and a relative import is resolved first.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parents[2] / "src"

#: Each bounded package, and the third-party distributions it may import besides the standard library.
_BOUNDED: dict[str, frozenset[str]] = {
    "headgate._core": frozenset({"httpx2"}),
    "headgate.breaker": frozenset({"httpx2"}),
    "headgate.ratelimit": frozenset({"httpx2", "aiolimiter"}),
    "headgate.retry": frozenset({"httpx2"}),
}

#: What every bounded package other than the core may import of headgate, besides itself.
_SHARED_CORE = "headgate._core"


def _imported(tree: ast.Module, module: str) -> list[tuple[int, str]]:
    """Return ``(line, module)`` for every import in *tree*, a module named *module*, relative ones resolved."""
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend((node.lineno, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                parent = module.split(".")[: -node.level]
                base = ".".join(parent if node.module is None else [*parent, node.module])
            else:
                base = node.module or ""
            # ``from headgate import retry`` imports a module of headgate's as surely as ``import headgate.retry``.
            found.extend((node.lineno, f"{base}.{alias.name}") for alias in node.names)
    return found


def _within(name: str, package: str) -> bool:
    """Return whether module *name* is *package* or inside it."""
    return name == package or name.startswith(f"{package}.")


def violations(package: str, module: str, source: str) -> list[str]:
    """Return a line per import in *source* (module *module* of bounded *package*) that crosses its boundary."""
    third_party = _BOUNDED[package]
    allowed_headgate = {package, _SHARED_CORE}
    bad: list[str] = []
    for line, name in _imported(ast.parse(source), module):
        top = name.partition(".")[0]
        if top == "__future__" or top in sys.stdlib_module_names or top in third_party:
            continue
        if top == "headgate" and any(_within(name, allowed) for allowed in allowed_headgate):
            continue
        bad.append(f"{module}:{line}: imports {name}")
    return bad


def _modules(package: str) -> list[tuple[str, Path]]:
    """Return every module of *package* in the source tree, with its dotted name.

    A package's ``__init__`` keeps that last part (``headgate.retry.__init__``), so a relative import in it resolves
    against the package itself, as Python resolves it.
    """
    root = _SRC.joinpath(*package.split("."))
    return [(".".join(path.relative_to(_SRC).with_suffix("").parts), path) for path in sorted(root.rglob("*.py"))]


@pytest.mark.parametrize("package", sorted(_BOUNDED))
def test_a_bounded_package_imports_only_what_its_boundary_allows(package: str) -> None:
    """Check every import of every module in *package* against its boundary."""
    modules = _modules(package)
    assert modules, f"{package} has no modules under {_SRC}"
    offenders = [
        bad for module, path in modules for bad in violations(package, module, path.read_text(encoding="utf-8"))
    ]
    assert not offenders, "\n".join(offenders)


@pytest.mark.parametrize(
    "source",
    [
        pytest.param("from headgate._client import AsyncBaseClient\n", id="the-client"),
        pytest.param("import headgate._stack\n", id="the-stack"),
        pytest.param("from headgate import _cache\n", id="the-cache-by-from-import"),
        pytest.param("from headgate.breaker import CircuitBreaker\n", id="another-policy-package"),
        pytest.param("from ... import errors\n", id="a-relative-import-out"),
        pytest.param("if TYPE_CHECKING:\n    from headgate.ratelimit import Rate\n", id="a-type-checking-import"),
        pytest.param("import msgspec\n", id="an-undeclared-dependency"),
    ],
)
def test_the_check_catches_a_crossing(source: str) -> None:
    """Check that the boundary check reports each kind of crossing, so a passing run means something."""
    assert violations("headgate.retry", "headgate.retry._policy", source)


def test_the_check_allows_what_the_boundary_does() -> None:
    """Check that the standard library, httpx2, the core and the package's own modules pass."""
    source = (
        "from __future__ import annotations\nimport asyncio\nimport httpx2\nfrom headgate._core import CallContext\n"
        "from headgate.retry._policy import RetryPolicy\nfrom . import _policy\nfrom ._policy import NO_RETRY\n"
    )
    assert violations("headgate.retry", "headgate.retry._transport", source) == []
