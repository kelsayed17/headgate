# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""The README's contracts that a grep can check: no logging, no environment reads, no upstream ``httpx``."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).parents[2]
_SHIPPED = sorted((_ROOT / "src").rglob("*.py")) + sorted((_ROOT / "examples").rglob("*.py"))

_FORBIDDEN = {
    "logging": re.compile(r"^\s*(import|from)\s+(logging|structlog)\b|getLogger"),
    "environment reads": re.compile(r"\bos\.environ\b|\bgetenv\b"),
    "upstream httpx": re.compile(r"^\s*(import|from)\s+httpx\b(?!2)"),
}


@pytest.mark.parametrize("contract", sorted(_FORBIDDEN))
def test_shipped_code_keeps_the_contract(contract: str) -> None:
    """Check that no shipped line logs, reads the environment, or imports upstream ``httpx``."""
    pattern = _FORBIDDEN[contract]
    offenders = [
        f"{path.relative_to(_ROOT)}:{n}: {line.strip()}"
        for path in _SHIPPED
        for n, line in enumerate(path.read_text().splitlines(), 1)
        if pattern.search(line)
    ]
    assert not offenders, "\n".join(offenders)
