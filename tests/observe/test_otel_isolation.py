# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""OpenTelemetry stays optional: the core never imports it, and ``headgate.otel`` says what to install without it.

Each test re-imports headgate from scratch in this process with ``opentelemetry`` made unimportable (a ``None`` in
``sys.modules`` makes ``import`` raise). ``monkeypatch`` restores every ``sys.modules`` entry afterwards, so the rest
of the suite keeps the modules it already imported.
"""

from __future__ import annotations

import importlib
import sys

import pytest


def _block_opentelemetry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Forget every imported headgate and opentelemetry module, and make ``opentelemetry`` fail to import."""
    for name in list(sys.modules):
        if any(name == root or name.startswith(f"{root}.") for root in ("headgate", "opentelemetry")):
            monkeypatch.delitem(sys.modules, name)
    monkeypatch.setitem(sys.modules, "opentelemetry", None)


@pytest.mark.parametrize(
    "module", ["headgate", "headgate._client", "headgate._core", "headgate.events", "headgate.testing"]
)
def test_the_core_never_imports_opentelemetry(monkeypatch: pytest.MonkeyPatch, module: str) -> None:
    """Check that *module* imports with OpenTelemetry unavailable."""
    _block_opentelemetry(monkeypatch)
    importlib.import_module(module)


def test_the_otel_module_names_the_extra_it_needs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Raise an ``ImportError`` that names the ``otel`` extra, chained to the missing module's."""
    _block_opentelemetry(monkeypatch)
    with pytest.raises(ImportError, match=r"pip install headgate\[otel\]") as caught:
        importlib.import_module("headgate.otel")
    assert isinstance(caught.value.__cause__, ImportError)
