"""What importing headgate pulls in: never upstream ``httpx``, and no codec unless a flavor module is imported."""

from __future__ import annotations

import subprocess
import sys

import pytest


def _import_with_blocked(module: str, *blocked: str) -> subprocess.CompletedProcess[str]:
    """Import *module* in a fresh interpreter in which every name in *blocked* fails to import."""
    script = "".join(f"import sys; sys.modules[{name!r}] = None\n" for name in blocked) + f"import {module}\n"
    return subprocess.run([sys.executable, "-I", "-c", script], capture_output=True, text=True, check=False)


@pytest.mark.parametrize("module", ["headgate", "headgate.msgspec", "headgate.health", "headgate.policy"])
def test_headgate_never_imports_upstream_httpx(module: str) -> None:
    result = _import_with_blocked(module, "httpx")
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("module", ["headgate", "headgate._client", "headgate._responses", "headgate.errors"])
def test_the_core_imports_neither_codec(module: str) -> None:
    result = _import_with_blocked(module, "msgspec", "pydantic")
    assert result.returncode == 0, result.stderr
