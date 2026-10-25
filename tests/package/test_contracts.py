"""The distribution is closed: nothing in it names the application it was extracted from or reads ambient state."""

from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).parents[2]
_APP = "pri" + "sm"  # the application this was extracted from, spelled so this file does not contain it
_FORBIDDEN = re.compile(
    rf"{_APP}|^\s*(import|from)\s+httpx(?![\w-])|getenv|ibkr|\bCLAUDE\b|Example, Example|serde\.|\.ruff\.toml|brokerage/positions|\benviron\b",
    re.IGNORECASE,
)
#: headgate never logs. Tests may use stdlib logging to observe asyncio records, so this covers shipped code only.
_NO_LOGGING = re.compile(r"structlog|\blogging\b|getLogger", re.IGNORECASE)
_SELF = Path(__file__).resolve()


def test_no_forbidden_names() -> None:
    offenders = [
        f"{path.relative_to(_ROOT)}:{n}: {line.strip()}"
        for folder in ("src", "tests", "examples")
        for path in (_ROOT / folder).rglob("*.py")
        if path.resolve() != _SELF
        for n, line in enumerate(path.read_text().splitlines(), 1)
        if _FORBIDDEN.search(line) or (folder != "tests" and _NO_LOGGING.search(line))
    ]
    assert not offenders, "\n".join(offenders)
