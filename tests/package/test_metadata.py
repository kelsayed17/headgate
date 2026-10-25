# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""The built distribution's metadata: licence, Python floor, dependencies and extras."""

from __future__ import annotations

import re
from importlib.metadata import metadata, requires
from pathlib import Path

import headgate


def _requirements() -> list[str]:
    """Return the distribution's requirements, lowercased, without extras brackets or spaces."""
    return [re.sub(r"\[[^\]]*\]", "", r.lower()).replace(" ", "") for r in requires("headgate") or []]


def test_metadata_declares_licence_python_and_author() -> None:
    """Check the licence expression, the Python floor and the author."""
    meta = metadata("headgate")
    assert meta["License-Expression"] == "MIT"
    assert meta["Requires-Python"] == ">=3.14"
    assert "Khaled Elsayed" in meta["Author-email"]


def test_core_dependencies_are_httpx2_based_and_extras_are_optional() -> None:
    """Check that the core depends on httpx2 and never upstream httpx, and that curl and pydantic are extras."""
    reqs = _requirements()
    for needed in ("httpx2", "msgspec", "hishel"):
        assert any(r.startswith(needed) and "extra==" not in r for r in reqs), needed
    assert not any(re.match(r"httpx(?!2)", r) for r in reqs), "headgate must not depend on upstream httpx"
    assert any("extra=='curl'" in r and r.startswith("curl-cffi") for r in reqs)
    assert any("extra=='pydantic'" in r and r.startswith("pydantic") for r in reqs)


def test_the_package_is_marked_typed() -> None:
    """Check that the package ships ``py.typed``."""
    assert (Path(headgate.__file__).parent / "py.typed").is_file()
