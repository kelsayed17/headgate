# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""The guides' marked code samples type-check under the project's own rules, and the ones marked to run, run.

A sample is marked by an HTML comment on the line before its fence, which renders as nothing:

* ``<!-- docs-test: check -->``: the sample type-checks. Each page's marked samples, in order, form one module after
  ``from headgate import *`` and ``from tests.examples.doc_context import *`` (the domain types, values and SDKs a
  sample names without defining), a sample's statements from its first top-level ``await`` on wrapped in an
  ``async def``. One ``ty check`` runs over every page's module, with every rule an error, so a spelling the API no
  longer has fails here.
* ``<!-- docs-test: run -->``: the sample type-checks, and runs too. A page's module runs, in a temporary directory,
  with each sample marked to check cut at its first statement that awaits (what came before, its imports and
  definitions, later samples rely on), inside ``fake_network(FakeTransport())`` and ``TestClock().install()``, so a
  client built at import is on a fake network and nothing waits or reaches the internet. Every test function a run
  sample defines that takes no argument is then called, and awaited when it is async: the sample test of the codec
  guide runs ``check_codec`` on the guide's own codec.

The README's samples are excerpts of tested modules, checked by ``test_docs.py``; an unmarked guide sample is an
illustrative fragment, each listed with its reason in the phase 5 plan.
"""

from __future__ import annotations

import ast
import asyncio
import importlib.util
import inspect
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

import pytest

from headgate.testing import FakeTransport, TestClock, fake_network
from tests.examples import doc_context

if TYPE_CHECKING:
    from collections.abc import Iterator

_ROOT: Final = Path(__file__).resolve().parents[2]
_PAGES: Final = sorted((_ROOT / "docs").glob("*.md"))

#: A marker and the Python fence right after it: its mode and its body.
_MARKED: Final = re.compile(r"^<!-- docs-test: (\w+) -->\n```python\n(.*?)^```$", re.MULTILINE | re.DOTALL)

#: Any marker, whether or not a Python fence follows it.
_MARKER: Final = re.compile(r"<!-- docs-test:[^>]*-->")

#: What each page's module starts with, before its samples.
_HEADER: Final[str] = "from headgate import *\nfrom tests.examples.doc_context import *\n"

#: A diagnostic in ty's concise output: its file, line, and the rest.
_DIAGNOSTIC: Final = re.compile(r"^(?P<file>.+?):(?P<line>\d+):\d+: (?P<text>(?:error|warning)\[.*)$", re.MULTILINE)


@dataclass(frozen=True)
class _Sample:
    """One marked sample: its page, the page line its code starts on, its mode, and its code."""

    page: Path
    line: int
    mode: str
    code: str

    def split(self) -> tuple[list[str], list[str]]:
        """Return the sample's lines before its first statement that awaits, and the lines from there on.

        A statement awaits when it holds an ``await``, ``async for`` or ``async with`` outside any function: it needs
        a coroutine to type-check in, while the imports and definitions before it stay at the module's top level, where
        the page's later samples see them.
        """
        lines = self.code.splitlines()
        for statement in ast.parse(self.code).body:
            if _awaits(statement):
                return lines[: statement.lineno - 1], lines[statement.lineno - 1 :]
        return lines, []


def _awaits(node: ast.AST) -> bool:
    """Return whether *node* awaits outside a function of its own."""
    if isinstance(node, ast.Await | ast.AsyncFor | ast.AsyncWith):
        return True
    if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda | ast.ClassDef):
        return False
    return any(_awaits(child) for child in ast.iter_child_nodes(node))


def _samples(page: Path) -> list[_Sample]:
    """Return the marked samples of *page*, in order."""
    text = page.read_text(encoding="utf-8")
    return [
        _Sample(page, text.count("\n", 0, match.start(2)) + 1, match[1], match[2]) for match in _MARKED.finditer(text)
    ]


_SAMPLES: Final = {page: _samples(page) for page in _PAGES}
_MARKED_PAGES: Final = [page for page, samples in _SAMPLES.items() if samples]
_RUN_PAGES: Final = [page for page, samples in _SAMPLES.items() if any(s.mode == "run" for s in samples)]


def _module(samples: list[_Sample], *, run: bool = False) -> tuple[str, list[str]]:
    """Return the module *samples* make, and where each of its lines is in its page (``""`` for the module's own).

    The module to *run* leaves out what a sample only to check awaits: it needs a server.
    """
    lines: list[str] = _HEADER.splitlines()
    where: list[str] = [""] * len(lines)
    for sample in samples:
        top, awaiting = sample.split()
        if run and sample.mode == "check":
            awaiting = []
        lines.extend(["", "", *top])
        where.extend(["", "", *[f"{sample.page.name}:{sample.line + index}" for index in range(len(top))]])
        if awaiting:
            lines.append(f"async def _sample_{sample.line}() -> None:")
            where.append(f"{sample.page.name}:{sample.line + len(top)}")
            lines.extend(f"    {line}" if line else line for line in awaiting)
            where.extend(f"{sample.page.name}:{sample.line + len(top) + index}" for index in range(len(awaiting)))
    return "\n".join(lines) + "\n", where


@pytest.fixture(scope="module")
async def type_report(tmp_path_factory: pytest.TempPathFactory) -> dict[Path, list[str]]:
    """Type-check every marked page's module in one ``ty`` run; return each page's diagnostics, at their page lines."""
    root = tmp_path_factory.mktemp("doc_samples")
    modules: dict[Path, tuple[Path, list[str]]] = {}
    for page in _MARKED_PAGES:
        source, where = _module(_SAMPLES[page])
        module = root / f"docs_{page.stem.replace('-', '_')}.py"
        module.write_text(source, encoding="utf-8")
        modules[page] = (module, where)
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "ty",
        "check",
        "--output-format",
        "concise",
        *(str(module) for module, _ in modules.values()),
        cwd=_ROOT,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    output = stdout.decode()
    found: dict[Path, list[str]] = {page: [] for page in modules}
    by_file = {str(module): page for page, (module, _) in modules.items()}
    for match in _DIAGNOSTIC.finditer(output):
        page = by_file[match["file"]]
        line = modules[page][1][int(match["line"]) - 1] or f"{page.name}, the module's own line {match['line']}"
        found[page].append(f"{line}: {match['text']}")
    assert process.returncode == 0 or any(found.values()), output + stderr.decode()
    return found


def test_the_guides_mark_samples_to_check() -> None:
    """Find marked samples, of both modes, so an emptied marker set cannot pass the checks below unnoticed."""
    modes = {sample.mode for samples in _SAMPLES.values() for sample in samples}
    assert modes == {"run", "check"}


@pytest.mark.parametrize("page", _PAGES, ids=[page.name for page in _PAGES])
def test_every_marker_is_known_and_on_a_python_sample(page: Path) -> None:
    """Refuse a marker of another mode, or one not on the line before a Python fence, which would check nothing."""
    text = page.read_text(encoding="utf-8")
    assert len(_MARKER.findall(text)) == len(_SAMPLES[page]), f"{page.name}: a marker not before a Python fence"
    unknown = [f"{s.page.name}:{s.line}" for s in _SAMPLES[page] if s.mode not in {"run", "check"}]
    assert not unknown, f"markers of an unknown mode at {unknown}"


@pytest.mark.parametrize("page", _MARKED_PAGES, ids=[page.name for page in _MARKED_PAGES])
def test_every_marked_sample_type_checks(page: Path, type_report: dict[Path, list[str]]) -> None:
    """Type-check the page's marked samples, as one module, with every rule an error."""
    assert not type_report[page], "\n".join(type_report[page])


@pytest.fixture
def _fake_world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Run in a temporary directory, on a fake network and a test clock."""
    monkeypatch.chdir(tmp_path)
    with fake_network(FakeTransport()), TestClock().install():
        yield


@pytest.mark.usefixtures("_fake_world")
@pytest.mark.parametrize("page", _RUN_PAGES, ids=[page.name for page in _RUN_PAGES])
async def test_every_run_sample_runs(page: Path, tmp_path: Path) -> None:
    """Import the page's module to run, then await its wrapped samples and call its argument-free tests."""
    name = f"docs_{page.stem.replace('-', '_')}_run"
    source, _ = _module(_SAMPLES[page], run=True)
    path = tmp_path / f"{name}.py"
    path.write_text(source, encoding="utf-8")
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
        for value in list(vars(module).values()):
            # A sample with a top-level await, wrapped to type-check, runs here; so does a test a sample defines.
            if not _runnable(value, name):
                continue
            if inspect.isawaitable(result := value()):
                await result
    finally:
        del sys.modules[name]


def _runnable(value: object, module: str) -> bool:
    """Return whether *value* is a function *module* defines that the run calls: a wrapped sample, or a test."""
    return (
        inspect.isfunction(value)
        and value.__module__ == module
        and value.__name__.startswith(("_sample_", "test_"))
        and not inspect.signature(value).parameters
    )


def test_the_context_holds_every_name_it_lists() -> None:
    """Hold every name the context lists, so a sample's name resolves at runtime as it does for the checker."""
    assert [name for name in doc_context.__all__ if not hasattr(doc_context, name)] == []
