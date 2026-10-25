"""The core imports neither codec: a client picks msgspec or pydantic by importing a flavor module, nothing else."""

from __future__ import annotations

import subprocess
import sys

import pytest

#: The modules the codec seam promises are codec-free. ``headgate._client`` joins when the base client is ported.
_CORE = ["headgate._responses", "headgate._failures", "headgate.errors", "headgate._codec", "headgate._client"]

_SCRIPT = """
import sys
sys.modules["msgspec"] = None
sys.modules["pydantic"] = None
import {module}
"""


@pytest.mark.parametrize("module", _CORE)
def test_core_imports_with_codecs_blocked(module: str) -> None:
    result = subprocess.run(
        [sys.executable, "-I", "-c", _SCRIPT.format(module=module)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
