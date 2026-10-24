"""Importing headgate never touches upstream ``httpx``, and works whether or not httpx2 was imported first."""

from __future__ import annotations

import subprocess
import sys

import pytest

_MODULES = ["headgate.errors", "headgate._base", "headgate.msgspec"]


def _run(script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-I", "-c", script], capture_output=True, text=True, check=False)


@pytest.mark.parametrize("module", _MODULES)
def test_import_with_httpx_blocked(module: str) -> None:
    result = _run(f"import sys\nsys.modules['httpx'] = None\nimport {module}")
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("module", _MODULES)
def test_import_after_httpx2_already_imported(module: str) -> None:
    result = _run(f"import httpx2\nimport {module}")
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("module", _MODULES)
def test_import_before_httpx2(module: str) -> None:
    result = _run(f"import {module}\nimport httpx2")
    assert result.returncode == 0, result.stderr
