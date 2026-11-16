# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""The documentation's code samples are real code, and its links lead somewhere.

* Every Python block in ``README.md`` is an excerpt of a module in ``examples/`` or ``tests/examples/``: each of its
  top-level statements is one of that code's statements (a class may show only some of its members). Those modules are
  linted, type-checked and run by the tests beside this one, so a README sample that drifts from the API fails here.
* Every Python block in ``docs/`` parses, and every name it imports from headgate exists.
* Every dotted headgate name the prose of ``README.md`` and ``docs/`` spells out, such as ``headgate.health``, exists.
* Every relative link in ``README.md``, ``CHANGELOG.md`` and ``docs/`` names a file that exists, and a heading in it
  when it names one; so does every absolute link to the docs site, which the README uses so it works on PyPI too.
"""

from __future__ import annotations

import ast
import importlib
import pkgutil
import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_README = _ROOT / "README.md"
_DOCS = sorted((_ROOT / "docs").glob("*.md"))
_SOURCES = sorted([*(_ROOT / "examples").glob("*.py"), *(_ROOT / "tests" / "examples").glob("test_*.py")])

#: A fenced block: its language and its body.
_FENCE = re.compile(r"^```(\w*)\n(.*?)^```$", re.MULTILINE | re.DOTALL)

#: A Markdown link or image target, up to an optional title.
_LINK = re.compile(r"\]\(([^)\s]+)\)")

#: A link into the published docs site: the page's name (empty for the home page) and an optional anchor.
_SITE_LINK = re.compile(r"https://kelsayed17\.github\.io/headgate/([\w-]*)/?(?:#([\w-]+))?")

#: A dotted headgate name in inline code, such as ``headgate.testing.check_codec``.
_DOTTED_NAME = re.compile(r"`(headgate(?:\.\w+)+)`")

#: A heading line.
_HEADING = re.compile(r"^#{1,6} +(.+?) *$", re.MULTILINE)

#: Compile flags for a sample: an AST only, and top-level ``await`` allowed, as a sample shows a call in isolation.
_SAMPLE_FLAGS = ast.PyCF_ONLY_AST | ast.PyCF_ALLOW_TOP_LEVEL_AWAIT


def _python_blocks(path: Path) -> list[tuple[int, str]]:
    """Return each Python block of the Markdown file *path*, with the line its body starts on."""
    text = path.read_text(encoding="utf-8")
    return [
        (text.count("\n", 0, match.start()) + 2, match[2]) for match in _FENCE.finditer(text) if match[1] == "python"
    ]


def _parse(code: str, where: str) -> ast.Module:
    """Parse the sample *code* found at *where*."""
    tree = compile(code, where, "exec", flags=_SAMPLE_FLAGS)
    assert isinstance(tree, ast.Module)
    return tree


def _same(a: ast.AST, b: ast.AST) -> bool:
    """Return whether two nodes are the same code, wherever they sit in their files."""
    return ast.dump(a) == ast.dump(b)


def _class_head(node: ast.ClassDef) -> list[str]:
    """Return what identifies a class besides its body: its name, bases, keywords and decorators."""
    return [node.name, *map(ast.dump, (*node.bases, *node.keywords, *node.decorator_list))]


def _found(statement: ast.stmt, candidates: list[ast.stmt]) -> bool:
    """Return whether *statement* is one of *candidates*; a class matches one whose members include each of its own."""
    if isinstance(statement, ast.ClassDef):
        return any(
            isinstance(candidate, ast.ClassDef)
            and _class_head(candidate) == _class_head(statement)
            and all(_found(member, candidate.body) for member in statement.body)
            for candidate in candidates
        )
    return any(_same(statement, candidate) for candidate in candidates)


def _source_statements() -> list[ast.stmt]:
    """Return the top-level statements of every example and example test."""
    return [statement for path in _SOURCES for statement in ast.parse(path.read_text(encoding="utf-8")).body]


def _slug(heading: str) -> str:
    """Return the anchor GitHub gives *heading*: lower case, punctuation dropped, spaces as hyphens."""
    text = re.sub(r"[^\w\- ]", "", heading.lower().replace("`", ""))
    return text.replace(" ", "-")


def _anchors(path: Path) -> set[str]:
    """Return the heading anchors of the Markdown file *path*."""
    return {_slug(heading) for heading in _HEADING.findall(path.read_text(encoding="utf-8"))}


def _resolves(dotted: str) -> bool:
    """Return whether *dotted* names a headgate module, or an attribute reached from the longest module prefix.

    The first attribute after the module must be one the module exports, so a name spelled from a module that is not
    its one home (``headgate.ClientError``) does not resolve.
    """
    parts = dotted.split(".")
    for split in range(len(parts), 0, -1):
        try:
            found: object = importlib.import_module(".".join(parts[:split]))
        except ImportError:
            continue
        if split < len(parts) and parts[split] not in _exports(found):
            return False
        for part in parts[split:]:
            if not hasattr(found, part):
                return False
            found = getattr(found, part)
        return True
    return False


def _exports(module: object) -> set[str]:
    """Return the names *module* exports: its ``__all__``, or, for a module with none, its submodules' names only."""
    return set(getattr(module, "__all__", ())) | {
        info.name for info in pkgutil.iter_modules(getattr(module, "__path__", ())) if not info.name.startswith("_")
    }


def _misplaced(tree: ast.Module) -> list[str]:
    """Return each ``from headgate… import X`` in *tree* whose module does not export ``X``: not its one home."""
    return [
        f"{node.module}.{alias.name}"
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None and node.module.startswith("headgate")
        for alias in node.names
        if alias.name not in _exports(importlib.import_module(node.module))
    ]


_README_BLOCKS = _python_blocks(_README)
_DOC_BLOCKS = [(path, line, code) for path in _DOCS for line, code in _python_blocks(path)]
_PAGES = [_README, _ROOT / "CHANGELOG.md", *_DOCS]
#: The pages that describe the current API; the changelog names what was removed, so it is left out.
_CURRENT_PAGES = [_README, *_DOCS]


def test_the_docs_have_samples_to_check() -> None:
    """Find samples in the README and in the guides, so an emptied file cannot pass the checks below unnoticed."""
    assert _README_BLOCKS
    assert _DOC_BLOCKS
    assert _SOURCES


@pytest.mark.parametrize(("line", "code"), _README_BLOCKS, ids=[f"README.md:{line}" for line, _ in _README_BLOCKS])
def test_every_readme_sample_is_tested_code(line: int, code: str) -> None:
    """Find every top-level statement of the README sample at *line* in an example or an example test."""
    sources = _source_statements()
    missing = [
        ast.unparse(statement).splitlines()[0]
        for statement in _parse(code, f"README.md:{line}").body
        if not _found(statement, sources)
    ]
    assert not missing, f"README.md:{line}: not in examples/ or tests/examples/: {missing}"
    assert not _misplaced(_parse(code, f"README.md:{line}")), f"README.md:{line}: imported from outside its one home"


@pytest.mark.parametrize(
    ("path", "line", "code"), _DOC_BLOCKS, ids=[f"{path.name}:{line}" for path, line, _ in _DOC_BLOCKS]
)
def test_every_guide_sample_parses_and_imports_real_names(path: Path, line: int, code: str) -> None:
    """Parse the sample at *path*:*line*, and find every name it imports from headgate in the module it lives in."""
    tree = _parse(code, f"{path.name}:{line}")
    misplaced = _misplaced(tree)
    assert not misplaced, f"{path.name}:{line}: not exported from where it is imported: {misplaced}"
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("headgate"):
                    importlib.import_module(alias.name)


@pytest.mark.parametrize("page", _PAGES, ids=[page.name for page in _PAGES])
def test_every_relative_link_resolves(page: Path) -> None:
    """Find the file each relative link on *page* names, and the heading its anchor names."""
    broken: list[str] = []
    for target in _LINK.findall(page.read_text(encoding="utf-8")):
        if target.startswith(("http://", "https://", "mailto:")):
            continue
        file_part, _, anchor = target.partition("#")
        linked = page if not file_part else (page.parent / file_part).resolve()
        if not linked.exists() or (anchor and linked.suffix == ".md" and anchor not in _anchors(linked)):
            broken.append(target)
    assert not broken, f"{page.name}: broken links {broken}"


@pytest.mark.parametrize("page", _CURRENT_PAGES, ids=[page.name for page in _CURRENT_PAGES])
def test_every_dotted_name_in_the_prose_exists(page: Path) -> None:
    """Import or reach every ``headgate.…`` name *page* spells out in inline code."""
    missing = sorted({name for name in _DOTTED_NAME.findall(page.read_text(encoding="utf-8")) if not _resolves(name)})
    assert not missing, f"{page.name}: no such headgate names {missing}"


@pytest.mark.parametrize("page", _PAGES, ids=[page.name for page in _PAGES])
def test_every_docs_site_link_resolves(page: Path) -> None:
    """Find the guide each docs-site link on *page* names, and the heading its anchor names."""
    broken: list[str] = []
    for match in _SITE_LINK.finditer(page.read_text(encoding="utf-8")):
        guide = _ROOT / "docs" / f"{match[1] or 'index'}.md"
        if not guide.exists() or (match[2] and match[2] not in _anchors(guide)):
            broken.append(match[0])
    assert not broken, f"{page.name}: broken docs-site links {broken}"


def _public_names() -> list[str]:
    """Return every name in the ``__all__`` of headgate and of each of its public modules."""
    root = importlib.import_module("headgate")
    public = [f"headgate.{info.name}" for info in pkgutil.iter_modules(root.__path__) if not info.name.startswith("_")]
    modules = [root, *map(importlib.import_module, public)]
    return sorted({name for module in modules for name in module.__all__})


def test_every_public_name_is_in_the_docs() -> None:
    """Find every exported name in the README or a guide, so the docs, not only the API snapshot, show it exists."""
    prose = "\n".join(page.read_text(encoding="utf-8") for page in _CURRENT_PAGES)
    missing = [name for name in _public_names() if not re.search(rf"\b{re.escape(name)}\b", prose)]
    assert not missing, f"exported but in no guide or README: {missing}"


def test_every_rfc_the_source_cites_is_on_the_standards_page() -> None:
    """List each RFC a comment or docstring in ``src/`` cites on the standards page, so the page stays complete."""
    cited = {
        number
        for path in (_ROOT / "src").rglob("*.py")
        for number in re.findall(r"\bRFC ?(\d{3,5})\b", path.read_text(encoding="utf-8"))
    }
    standards = (_ROOT / "docs" / "standards.md").read_text(encoding="utf-8")
    missing = sorted(number for number in cited if not re.search(rf"\b(?:RFC ?|rfc){number}\b", standards))
    assert not missing, f"cited in src/ but not on docs/standards.md: RFC {missing}"
