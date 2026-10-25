# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""What importing headgate pulls in: never upstream ``httpx``, and no codec unless a flavor module is imported.

Each test re-imports headgate from scratch in this process, with the named modules made unimportable (a ``None`` in
``sys.modules`` makes ``import`` raise). ``monkeypatch`` restores every ``sys.modules`` entry afterwards, so the rest
of the suite keeps the headgate it already imported.
"""

from __future__ import annotations

import importlib
import sys

import pytest


def _import_with_blocked(monkeypatch: pytest.MonkeyPatch, module: str, *blocked: str) -> None:
    """Import *module* afresh while every name in *blocked*, and every module under it, fails to import."""
    for name in list(sys.modules):
        if any(name == root or name.startswith(f"{root}.") for root in ("headgate", *blocked)):
            monkeypatch.delitem(sys.modules, name)
    for name in blocked:
        monkeypatch.setitem(sys.modules, name, None)
    importlib.import_module(module)


@pytest.mark.parametrize("module", ["headgate", "headgate.msgspec", "headgate.health", "headgate.policy"])
def test_headgate_never_imports_upstream_httpx(monkeypatch: pytest.MonkeyPatch, module: str) -> None:
    """Check that *module* imports with upstream ``httpx`` unavailable."""
    _import_with_blocked(monkeypatch, module, "httpx")


@pytest.mark.parametrize("module", ["headgate", "headgate._client", "headgate._responses", "headgate.errors"])
def test_the_core_imports_neither_codec(monkeypatch: pytest.MonkeyPatch, module: str) -> None:
    """Check that the codec-free core imports with neither msgspec nor pydantic available."""
    _import_with_blocked(monkeypatch, module, "msgspec", "pydantic")
